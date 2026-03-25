from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Optional

from .current_space import (
    derive_workflow_space_from_window,
    query_eligible_windows,
    query_focused_window_record,
    query_window_record,
    resolve_current_space_target,
    validate_workflow_space,
)
from .eligibility import is_eligible_window
from .errors import WorkflowError
from .models import (
    AltTabSession,
    FocusGuard,
    ManagedSpaceState,
    ManagedTile,
    PendingSplit,
    RuntimeState,
)
from .state import RuntimeStateStore
from .yabai import YabaiClient

SUPPORTED_SPLIT_DIRECTIONS = frozenset({"east", "south"})
SUPPORTED_ALTTAB_CANCEL_REASONS = frozenset(
    {
        "chooser_close",
        "chooser_hide",
        "chooser_quit",
        "esc",
        "space",
        "thumbnail_click",
    }
)
SUPPORTED_SIGNAL_WINDOW_EVENTS = frozenset(
    {
        "window_created",
        "window_deminimized",
        "window_destroyed",
        "window_minimized",
        "window_moved",
    }
)
ARRIVAL_EVENTS = frozenset({"window_created", "window_deminimized"})
LOSS_EVENTS = frozenset({"window_destroyed", "window_minimized"})
DRIFT_ERROR = "Managed space drifted away from live yabai topology. Run `yhwm reseed`."


@dataclass
class _WorkingTile:
    tile_id: int
    visible_window_id: Optional[int]
    hidden_window_ids: list[int]

    def all_window_ids(self) -> list[int]:
        members: list[int] = []
        if self.visible_window_id is not None:
            members.append(self.visible_window_id)
        members.extend(self.hidden_window_ids)
        return members


@dataclass
class _WorkingSpace:
    workflow_space: Any
    tiles: dict[int, _WorkingTile]
    last_focused_tile_id: Optional[int]
    next_tile_id: int
    pending_split: Optional[PendingSplit]


@dataclass(frozen=True)
class _ReconciledSpace:
    working: _WorkingSpace
    live_window_records: dict[int, Mapping[str, Any]]
    unknown_live_window_ids: set[int]


class WorkflowRuntime:
    def __init__(self, *, yabai: YabaiClient, state_store: RuntimeStateStore):
        self._yabai = yabai
        self._state_store = state_store

    def float_space(self) -> None:
        state = self._state_store.read()
        focused_window = _query_focused_window_record_or_none(self._yabai)
        if focused_window is None:
            return

        workflow_space = derive_workflow_space_from_window(
            focused_window,
            description="focused window",
        )
        if not _validate_workflow_space_or_none(
            self._yabai,
            workflow_space=workflow_space,
            allowed_layouts=("bsp", "stack", "float"),
        ):
            return

        if not is_eligible_window(
            focused_window,
            target_display=workflow_space.display,
            target_space=workflow_space.space,
        ):
            return

        if workflow_space.storage_key not in state.spaces:
            return

        self._yabai.set_space_layout(workflow_space.space, "float")
        self._state_store.write(_delete_space_state(state, workflow_space))

    def delete_tile(self) -> None:
        state = self._state_store.read()
        target = resolve_current_space_target(
            self._yabai,
            allowed_layouts=("bsp",),
        )
        managed = state.spaces.get(target.workflow_space.storage_key)
        if managed is None:
            return

        eligible_windows = query_eligible_windows(
            self._yabai,
            workflow_space=target.workflow_space,
        )
        reconciled = self._reconcile_managed_space(managed, eligible_windows)
        working = self._require_tracked_focused_tile(
            state,
            managed=managed,
            reconciled=reconciled,
            focused_window_id=target.focused_window_id,
        )

        if len(working.tiles) <= 1:
            self._write_reconciled_space(state, working)
            return

        focused_tile = _require_tile_for_window(working, target.focused_window_id)
        anchor_tile = _choose_delete_anchor_tile(
            self._yabai,
            working=working,
            deleted_tile_id=focused_tile.tile_id,
        )
        if anchor_tile is None:
            self._write_reconciled_space(state, working)
            return

        transfer_window_ids = focused_tile.all_window_ids()
        if anchor_tile.visible_window_id is None:
            self._clear_space_and_fail(state, target.workflow_space)
        anchor_visible_window_id = anchor_tile.visible_window_id
        for window_id in transfer_window_ids:
            self._yabai.stack_window(anchor_visible_window_id, window_id)
        self._yabai.focus_window(anchor_visible_window_id)

        anchor_tile.hidden_window_ids = _dedupe_window_ids(
            [*anchor_tile.hidden_window_ids, *transfer_window_ids]
        )
        working.tiles.pop(focused_tile.tile_id, None)
        if working.pending_split is not None and working.pending_split.tile_id == focused_tile.tile_id:
            working.pending_split = None
        working.last_focused_tile_id = anchor_tile.tile_id
        self._write_reconciled_space(state, working)

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

        managed = ManagedSpaceState(
            workflow_space=target.workflow_space,
            tiles={
                1: ManagedTile(
                    tile_id=1,
                    visible_window_id=target.focused_window_id,
                    hidden_window_ids=hidden_window_ids,
                )
            },
            last_focused_tile_id=1,
            next_tile_id=2,
            pending_split=None,
        )
        self._state_store.write(
            RuntimeState(
                spaces={**state.spaces, target.workflow_space.storage_key: managed},
                alttab_session=None,
                focus_guard=None,
            )
        )

    def split(self, direction: str) -> None:
        normalized_direction = direction.strip().lower()
        if normalized_direction not in SUPPORTED_SPLIT_DIRECTIONS:
            raise WorkflowError(f"Unsupported split direction: {direction}")

        state = self._state_store.read()
        target = resolve_current_space_target(
            self._yabai,
            allowed_layouts=("bsp",),
        )
        managed = state.spaces.get(target.workflow_space.storage_key)
        if managed is None:
            raise WorkflowError("Current space is not tracked. Run `yhwm reseed` first.")

        eligible_windows = query_eligible_windows(
            self._yabai,
            workflow_space=target.workflow_space,
        )
        reconciled = self._reconcile_managed_space(managed, eligible_windows)
        working = self._require_tracked_focused_tile(
            state,
            managed=managed,
            reconciled=reconciled,
            focused_window_id=target.focused_window_id,
        )
        focused_tile = _require_tile_for_window(working, target.focused_window_id)
        focused_visible_window_id = focused_tile.visible_window_id
        if focused_visible_window_id is None:
            self._clear_space_and_fail(state, target.workflow_space)

        candidate = _choose_split_candidate(working, focused_tile.tile_id)
        if candidate is None:
            self._yabai.arm_window_split(focused_visible_window_id, normalized_direction)
            working.pending_split = PendingSplit(
                tile_id=focused_tile.tile_id,
                direction=normalized_direction,
            )
            working.last_focused_tile_id = focused_tile.tile_id
            self._write_reconciled_space(state, working)
            return

        source_tile = _require_tile_for_window(working, candidate)
        source_tile.hidden_window_ids = [
            window_id
            for window_id in source_tile.hidden_window_ids
            if window_id != candidate
        ]

        if source_tile.tile_id == focused_tile.tile_id:
            self._yabai.promote_stacked_window(candidate, normalized_direction)
        else:
            self._yabai.arm_window_split(focused_visible_window_id, normalized_direction)
            self._yabai.warp_window(candidate, focused_visible_window_id)

        new_tile_id = working.next_tile_id
        working.next_tile_id += 1
        working.tiles[new_tile_id] = _WorkingTile(
            tile_id=new_tile_id,
            visible_window_id=candidate,
            hidden_window_ids=[],
        )
        working.pending_split = None
        working.last_focused_tile_id = focused_tile.tile_id
        self._write_reconciled_space(state, working)

    def handle_focus(self, window_id: int) -> None:
        self._handle_live_signal(event="window_focused", window_id=window_id)

    def handle_window_event(self, *, event: str, window_id: int) -> None:
        if event not in SUPPORTED_SIGNAL_WINDOW_EVENTS:
            raise WorkflowError(f"Unsupported window event: {event}")
        self._handle_live_signal(event=event, window_id=window_id)

    def alttab_open(self) -> None:
        state = self._state_store.read()
        if state.alttab_session is not None:
            return

        focused_window = query_focused_window_record(
            self._yabai,
            description="focused window",
        )
        workflow_space = derive_workflow_space_from_window(
            focused_window,
            description="focused window",
        )
        focused_window_id = int(focused_window["id"])
        if not is_eligible_window(
            focused_window,
            target_display=workflow_space.display,
            target_space=workflow_space.space,
        ):
            return

        managed = state.spaces.get(workflow_space.storage_key)
        if managed is None:
            return

        validate_workflow_space(
            self._yabai,
            workflow_space=workflow_space,
            allowed_layouts=("bsp",),
        )
        eligible_windows = query_eligible_windows(
            self._yabai,
            workflow_space=workflow_space,
        )
        reconciled = self._reconcile_managed_space(managed, eligible_windows)
        working = self._require_tracked_focused_tile(
            state,
            managed=managed,
            reconciled=reconciled,
            focused_window_id=focused_window_id,
        )
        origin_tile = _require_tile_for_window(working, focused_window_id)
        finalized = _finalize_working_space(working)
        if finalized is None:
            self._clear_space_and_fail(state, workflow_space)
        self._state_store.write(
            replace(
                _replace_space_state(state, finalized),
                alttab_session=AltTabSession(
                    origin_window_id=focused_window_id,
                    origin_tile_id=origin_tile.tile_id,
                    origin_workflow_space=workflow_space,
                ),
                focus_guard=None,
            )
        )

    def alttab_release(self, window_id: Optional[int]) -> None:
        state = self._state_store.read()
        session = state.alttab_session
        if session is None:
            return

        selected_window_id = window_id
        if selected_window_id is None:
            focused_window = _query_focused_window_record_or_none(self._yabai)
            if focused_window is not None:
                selected_window_id = int(focused_window["id"])

        cleared_state = replace(state, alttab_session=None, focus_guard=None)
        if selected_window_id is None or selected_window_id == session.origin_window_id:
            self._state_store.write(cleared_state)
            return

        managed = cleared_state.spaces.get(session.origin_workflow_space.storage_key)
        if managed is None:
            self._state_store.write(cleared_state)
            return

        selected_window = _query_window_record_or_none(self._yabai, selected_window_id)
        if selected_window is None:
            self._state_store.write(cleared_state)
            return

        selected_workflow_space = derive_workflow_space_from_window(
            selected_window,
            description=f"selected window {selected_window_id}",
        )
        if selected_workflow_space != session.origin_workflow_space:
            self._state_store.write(cleared_state)
            return

        if not is_eligible_window(
            selected_window,
            target_display=selected_workflow_space.display,
            target_space=selected_workflow_space.space,
        ):
            self._state_store.write(cleared_state)
            return

        eligible_windows = query_eligible_windows(
            self._yabai,
            workflow_space=session.origin_workflow_space,
        )
        reconciled = self._reconcile_managed_space(managed, eligible_windows)
        if reconciled.unknown_live_window_ids:
            self._state_store.write(_delete_space_state(cleared_state, session.origin_workflow_space))
            return

        working = reconciled.working
        origin_tile = working.tiles.get(session.origin_tile_id)
        if origin_tile is None:
            self._state_store.write(_delete_space_state(cleared_state, session.origin_workflow_space))
            return

        if origin_tile.visible_window_id != session.origin_window_id:
            origin_tile = _find_tile_for_window(working, session.origin_window_id)
            if origin_tile is None:
                self._state_store.write(_delete_space_state(cleared_state, session.origin_workflow_space))
                return
            _promote_window_in_tile(origin_tile, session.origin_window_id)

        selected_tile = _find_tile_for_window(working, selected_window_id)
        if selected_tile is None:
            self._state_store.write(_delete_space_state(cleared_state, session.origin_workflow_space))
            return

        if selected_tile.tile_id == origin_tile.tile_id:
            if origin_tile.visible_window_id != selected_window_id:
                self._yabai.focus_window(selected_window_id)
                _promote_window_in_tile(origin_tile, selected_window_id)
            working.last_focused_tile_id = origin_tile.tile_id
            self._write_reconciled_space(cleared_state, working)
            return

        if selected_tile.visible_window_id == selected_window_id:
            if origin_tile.visible_window_id is None:
                self._state_store.write(_delete_space_state(cleared_state, session.origin_workflow_space))
                return
            self._yabai.swap_window(origin_tile.visible_window_id, selected_window_id)
            self._yabai.focus_window(selected_window_id)
            origin_visible = origin_tile.visible_window_id
            origin_tile.visible_window_id = selected_window_id
            selected_tile.visible_window_id = origin_visible
            working.last_focused_tile_id = origin_tile.tile_id
            self._write_reconciled_space(cleared_state, working)
            return

        if origin_tile.visible_window_id is None:
            self._state_store.write(_delete_space_state(cleared_state, session.origin_workflow_space))
            return
        self._yabai.stack_window(origin_tile.visible_window_id, selected_window_id)
        self._yabai.focus_window(selected_window_id)
        selected_tile.hidden_window_ids = [
            window
            for window in selected_tile.hidden_window_ids
            if window != selected_window_id
        ]
        previous_visible_window_id = origin_tile.visible_window_id
        origin_tile.visible_window_id = selected_window_id
        origin_tile.hidden_window_ids = _dedupe_window_ids(
            [previous_visible_window_id, *origin_tile.hidden_window_ids]
        )
        working.last_focused_tile_id = origin_tile.tile_id
        self._write_reconciled_space(cleared_state, working)

    def alttab_cancel(self, *, reason: str, window_id: Optional[int]) -> None:
        if reason not in SUPPORTED_ALTTAB_CANCEL_REASONS:
            raise WorkflowError(f"Unsupported AltTab cancel reason: {reason}")

        state = self._state_store.read()
        session = state.alttab_session
        if session is None:
            return

        focus_guard = None
        if window_id is not None:
            focus_guard = FocusGuard(
                workflow_space=session.origin_workflow_space,
                target_window_id=window_id,
            )

        self._state_store.write(
            replace(
                state,
                alttab_session=None,
                focus_guard=focus_guard,
            )
        )

    def _handle_live_signal(self, *, event: str, window_id: int) -> None:
        state = self._state_store.read()
        if event == "window_focused" and state.alttab_session is not None:
            return

        current_window = _query_window_record_or_none(self._yabai, window_id)
        current_workflow_space = _derive_workflow_space_or_none(
            current_window,
            description=f"signal window {window_id}",
        )
        current_window_is_eligible = (
            current_window is not None
            and current_workflow_space is not None
            and is_eligible_window(
                current_window,
                target_display=current_workflow_space.display,
                target_space=current_workflow_space.space,
            )
        )

        if (
            event == "window_focused"
            and current_window_is_eligible
            and state.focus_guard is not None
            and _focus_guard_matches(
                state.focus_guard,
                workflow_space=current_workflow_space,
                window_id=window_id,
            )
        ):
            self._state_store.write(replace(state, focus_guard=None))
            return

        resolved = self._resolve_managed_signal_space(
            state,
            event=event,
            window_id=window_id,
            current_workflow_space=current_workflow_space,
            current_window_is_eligible=current_window_is_eligible,
        )
        if resolved is None:
            return

        managed, workflow_space = resolved
        eligible_windows = query_eligible_windows(
            self._yabai,
            workflow_space=workflow_space,
        )
        reconciled = self._reconcile_managed_space(
            managed,
            eligible_windows,
        )
        if event == "window_focused" or event in ARRIVAL_EVENTS:
            working = self._apply_arrival_batch(
                state,
                workflow_space=workflow_space,
                reconciled=reconciled,
                event_window_id=window_id,
            )
            if working is None:
                return
        else:
            if reconciled.unknown_live_window_ids:
                self._state_store.write(_delete_space_state(state, workflow_space))
                return
            working = reconciled.working

        if not self._apply_focus_reconciliation(
            state,
            workflow_space=workflow_space,
            working=working,
        ):
            return

        next_state = replace(state, focus_guard=None) if event == "window_focused" else state
        self._write_reconciled_space(next_state, working)

    def _resolve_managed_signal_space(
        self,
        state: RuntimeState,
        *,
        event: str,
        window_id: int,
        current_workflow_space,
        current_window_is_eligible: bool,
    ) -> Optional[tuple[ManagedSpaceState, Any]]:
        if (
            (event == "window_focused" or event in ARRIVAL_EVENTS)
            and current_window_is_eligible
            and current_workflow_space is not None
        ):
            managed = state.spaces.get(current_workflow_space.storage_key)
            if managed is not None:
                return managed, current_workflow_space

        match = _find_space_and_tile_for_window(state, window_id)
        if match is None:
            return None
        managed, _, _ = match
        return managed, managed.workflow_space

    def _apply_arrival_batch(
        self,
        state: RuntimeState,
        *,
        workflow_space,
        reconciled: _ReconciledSpace,
        event_window_id: int,
    ) -> Optional[_WorkingSpace]:
        working = reconciled.working
        if not reconciled.unknown_live_window_ids:
            return working

        arrival_window_ids = _order_live_window_ids(
            reconciled.live_window_records,
            reconciled.unknown_live_window_ids,
        )
        visible_arrival_window_id = _choose_arrival_visible_window_id(
            self._yabai,
            workflow_space=workflow_space,
            ordered_arrival_window_ids=arrival_window_ids,
            event_window_id=event_window_id,
        )
        hidden_arrival_window_ids = [
            candidate
            for candidate in arrival_window_ids
            if candidate != visible_arrival_window_id
        ]

        if working.pending_split is not None and working.pending_split.tile_id in working.tiles:
            for candidate in hidden_arrival_window_ids:
                self._yabai.stack_window(visible_arrival_window_id, candidate)
            new_tile_id = working.next_tile_id
            working.next_tile_id += 1
            working.tiles[new_tile_id] = _WorkingTile(
                tile_id=new_tile_id,
                visible_window_id=visible_arrival_window_id,
                hidden_window_ids=hidden_arrival_window_ids,
            )
            working.pending_split = None
            working.last_focused_tile_id = new_tile_id
            return working

        target_tile = _choose_last_focused_tile(working)
        if target_tile is None or target_tile.visible_window_id is None:
            self._state_store.write(_delete_space_state(state, workflow_space))
            return None

        anchor_window_id = target_tile.visible_window_id
        for candidate in arrival_window_ids:
            self._yabai.stack_window(anchor_window_id, candidate)
        self._yabai.focus_window(visible_arrival_window_id)
        target_tile.visible_window_id = visible_arrival_window_id
        target_tile.hidden_window_ids = _dedupe_window_ids(
            [
                anchor_window_id,
                *target_tile.hidden_window_ids,
                *hidden_arrival_window_ids,
            ]
        )
        working.last_focused_tile_id = target_tile.tile_id
        return working

    def _apply_focus_reconciliation(
        self,
        state: RuntimeState,
        *,
        workflow_space,
        working: _WorkingSpace,
    ) -> bool:
        focused_window_id = _current_focused_window_id(
            self._yabai,
            workflow_space=workflow_space,
        )
        if focused_window_id is None:
            return True

        tile = _find_tile_for_window(working, focused_window_id)
        if tile is None:
            self._state_store.write(_delete_space_state(state, workflow_space))
            return False

        _promote_window_in_tile(tile, focused_window_id)
        working.last_focused_tile_id = tile.tile_id
        return True

    def _reconcile_managed_space(
        self,
        managed: ManagedSpaceState,
        eligible_windows: Iterable[Mapping[str, Any]],
        *,
        allowed_unknown_window_ids: Iterable[int] = (),
    ) -> _ReconciledSpace:
        live_window_records = {
            int(window["id"]): dict(window)
            for window in eligible_windows
        }
        allowed_unknown = set(allowed_unknown_window_ids)

        tiles: dict[int, _WorkingTile] = {}
        tracked_live_window_ids: set[int] = set()
        for tile_id, tile in sorted(managed.tiles.items()):
            visible_window_id = (
                tile.visible_window_id
                if tile.visible_window_id in live_window_records
                else None
            )
            hidden_window_ids = [
                window_id
                for window_id in tile.hidden_window_ids
                if window_id in live_window_records and window_id != visible_window_id
            ]
            if visible_window_id is None and not hidden_window_ids:
                continue
            tiles[tile_id] = _WorkingTile(
                tile_id=tile.tile_id,
                visible_window_id=visible_window_id,
                hidden_window_ids=hidden_window_ids,
            )
            tracked_live_window_ids.update(tiles[tile_id].all_window_ids())

        working = _WorkingSpace(
            workflow_space=managed.workflow_space,
            tiles=tiles,
            last_focused_tile_id=(
                managed.last_focused_tile_id
                if managed.last_focused_tile_id in tiles
                else None
            ),
            next_tile_id=max(managed.next_tile_id, max(tiles, default=0) + 1),
            pending_split=(
                managed.pending_split
                if managed.pending_split is not None and managed.pending_split.tile_id in tiles
                else None
            ),
        )
        unknown_live_window_ids = (
            set(live_window_records) - tracked_live_window_ids - allowed_unknown
        )
        return _ReconciledSpace(
            working=working,
            live_window_records=live_window_records,
            unknown_live_window_ids=unknown_live_window_ids,
        )

    def _require_tracked_focused_tile(
        self,
        state: RuntimeState,
        *,
        managed: ManagedSpaceState,
        reconciled: _ReconciledSpace,
        focused_window_id: int,
    ) -> _WorkingSpace:
        if reconciled.unknown_live_window_ids:
            self._clear_space_and_fail(state, managed.workflow_space)

        working = reconciled.working
        tile = _find_tile_for_window(working, focused_window_id)
        if tile is None:
            self._clear_space_and_fail(state, managed.workflow_space)
        _promote_window_in_tile(tile, focused_window_id)
        if any(candidate.visible_window_id is None for candidate in working.tiles.values()):
            self._clear_space_and_fail(state, managed.workflow_space)
        working.last_focused_tile_id = tile.tile_id
        return working

    def _write_reconciled_space(self, state: RuntimeState, working: _WorkingSpace) -> None:
        managed = _finalize_working_space(working)
        if managed is None:
            self._state_store.write(_delete_space_state(state, working.workflow_space))
            return
        self._state_store.write(_replace_space_state(state, managed))

    def _clear_space_and_fail(self, state: RuntimeState, workflow_space) -> None:
        self._state_store.write(_delete_space_state(state, workflow_space))
        raise WorkflowError(DRIFT_ERROR)


def _replace_space_state(state: RuntimeState, managed: ManagedSpaceState) -> RuntimeState:
    next_spaces = dict(state.spaces)
    next_spaces[managed.workflow_space.storage_key] = managed
    return replace(state, spaces=next_spaces)


def _delete_space_state(state: RuntimeState, workflow_space) -> RuntimeState:
    next_spaces = dict(state.spaces)
    next_spaces.pop(workflow_space.storage_key, None)
    next_state = replace(state, spaces=next_spaces)
    if (
        next_state.alttab_session is not None
        and next_state.alttab_session.origin_workflow_space == workflow_space
    ):
        next_state = replace(next_state, alttab_session=None)
    if (
        next_state.focus_guard is not None
        and next_state.focus_guard.workflow_space == workflow_space
    ):
        next_state = replace(next_state, focus_guard=None)
    return next_state


def _finalize_working_space(working: _WorkingSpace) -> Optional[ManagedSpaceState]:
    finalized_tiles: dict[int, ManagedTile] = {}
    for tile_id, tile in sorted(working.tiles.items()):
        visible_window_id = tile.visible_window_id
        hidden_window_ids = list(tile.hidden_window_ids)
        if visible_window_id is None:
            if not hidden_window_ids:
                continue
            visible_window_id = hidden_window_ids.pop(0)
        hidden_window_ids = [
            window_id
            for window_id in _dedupe_window_ids(hidden_window_ids)
            if window_id != visible_window_id
        ]
        finalized_tiles[tile_id] = ManagedTile(
            tile_id=tile_id,
            visible_window_id=visible_window_id,
            hidden_window_ids=hidden_window_ids,
        )

    if not finalized_tiles:
        return None

    last_focused_tile_id = working.last_focused_tile_id
    if last_focused_tile_id not in finalized_tiles:
        last_focused_tile_id = min(finalized_tiles)

    next_tile_id = max(working.next_tile_id, max(finalized_tiles) + 1)
    pending_split = working.pending_split
    if pending_split is not None and pending_split.tile_id not in finalized_tiles:
        pending_split = None

    return ManagedSpaceState(
        workflow_space=working.workflow_space,
        tiles=finalized_tiles,
        last_focused_tile_id=last_focused_tile_id,
        next_tile_id=next_tile_id,
        pending_split=pending_split,
    )


def _find_space_and_tile_for_window(
    state: RuntimeState,
    window_id: int,
) -> Optional[tuple[ManagedSpaceState, ManagedTile, bool]]:
    for managed in state.spaces.values():
        for tile in managed.tiles.values():
            if tile.visible_window_id == window_id:
                return managed, tile, True
            if window_id in tile.hidden_window_ids:
                return managed, tile, False
    return None


def _find_tile_for_window(working: _WorkingSpace, window_id: int) -> Optional[_WorkingTile]:
    for tile in working.tiles.values():
        if tile.visible_window_id == window_id or window_id in tile.hidden_window_ids:
            return tile
    return None


def _require_tile_for_window(working: _WorkingSpace, window_id: int) -> _WorkingTile:
    tile = _find_tile_for_window(working, window_id)
    if tile is None:
        raise WorkflowError(DRIFT_ERROR)
    return tile


def _promote_window_in_tile(tile: _WorkingTile, window_id: int) -> None:
    if tile.visible_window_id == window_id:
        return
    if window_id not in tile.hidden_window_ids:
        return
    remaining_hidden = [
        candidate
        for candidate in tile.hidden_window_ids
        if candidate != window_id
    ]
    if tile.visible_window_id is None:
        tile.visible_window_id = window_id
        tile.hidden_window_ids = remaining_hidden
        return
    tile.hidden_window_ids = [tile.visible_window_id, *remaining_hidden]
    tile.visible_window_id = window_id


def _choose_last_focused_tile(working: _WorkingSpace) -> Optional[_WorkingTile]:
    if working.last_focused_tile_id in working.tiles:
        return working.tiles[working.last_focused_tile_id]
    return _choose_first_tile(working)


def _choose_first_tile(working: _WorkingSpace) -> Optional[_WorkingTile]:
    if not working.tiles:
        return None
    return working.tiles[min(working.tiles)]


def _choose_delete_anchor_tile(
    yabai: YabaiClient,
    *,
    working: _WorkingSpace,
    deleted_tile_id: int,
) -> Optional[_WorkingTile]:
    recent_window = _query_recent_window_or_none(yabai)
    if recent_window is not None:
        recent_tile = _find_tile_for_window(working, int(recent_window["id"]))
        if recent_tile is not None and recent_tile.tile_id != deleted_tile_id:
            _promote_window_in_tile(recent_tile, int(recent_window["id"]))
            return recent_tile

    for tile_id in sorted(working.tiles):
        if tile_id == deleted_tile_id:
            continue
        return working.tiles[tile_id]
    return None


def _choose_split_candidate(working: _WorkingSpace, focused_tile_id: int) -> Optional[int]:
    focused_tile = working.tiles.get(focused_tile_id)
    if focused_tile is not None and focused_tile.hidden_window_ids:
        return focused_tile.hidden_window_ids[0]

    for tile_id in sorted(working.tiles):
        if tile_id == focused_tile_id:
            continue
        tile = working.tiles[tile_id]
        if tile.hidden_window_ids:
            return tile.hidden_window_ids[0]
    return None


def _dedupe_window_ids(window_ids: Iterable[int]) -> list[int]:
    result: list[int] = []
    for window_id in window_ids:
        if window_id not in result:
            result.append(window_id)
    return result


def _order_live_window_ids(
    live_window_records: Mapping[int, Mapping[str, Any]],
    window_ids: Iterable[int],
) -> list[int]:
    target_window_ids = set(window_ids)
    ordered = [
        window_id
        for window_id in live_window_records
        if window_id in target_window_ids
    ]
    for window_id in sorted(target_window_ids):
        if window_id not in ordered:
            ordered.append(window_id)
    return ordered


def _choose_arrival_visible_window_id(
    yabai: YabaiClient,
    *,
    workflow_space,
    ordered_arrival_window_ids: list[int],
    event_window_id: int,
) -> int:
    focused_window_id = _current_focused_window_id(
        yabai,
        workflow_space=workflow_space,
    )
    if focused_window_id in ordered_arrival_window_ids:
        return focused_window_id
    if event_window_id in ordered_arrival_window_ids:
        return event_window_id
    return ordered_arrival_window_ids[0]


def _current_focused_window_id(yabai: YabaiClient, *, workflow_space) -> Optional[int]:
    focused_window = _query_focused_window_record_or_none(yabai)
    if focused_window is None:
        return None
    focused_workflow_space = derive_workflow_space_from_window(
        focused_window,
        description="focused window",
    )
    if focused_workflow_space != workflow_space:
        return None
    focused_window_id = int(focused_window["id"])
    if not is_eligible_window(
        focused_window,
        target_display=workflow_space.display,
        target_space=workflow_space.space,
    ):
        return None
    return focused_window_id


def _derive_workflow_space_or_none(
    window: Optional[Mapping[str, Any]],
    *,
    description: str,
):
    if window is None:
        return None
    try:
        return derive_workflow_space_from_window(
            window,
            description=description,
        )
    except WorkflowError:
        return None


def _focus_guard_matches(
    focus_guard: FocusGuard,
    *,
    workflow_space,
    window_id: int,
) -> bool:
    if focus_guard.workflow_space != workflow_space:
        return False
    if focus_guard.target_window_id is None:
        return True
    return focus_guard.target_window_id == window_id


def _derive_workflow_space_or_none(
    window: Optional[Mapping[str, Any]],
    *,
    description: str,
):
    if window is None:
        return None
    try:
        return derive_workflow_space_from_window(
            window,
            description=description,
        )
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
        payload = yabai.query_recent_window()
    except WorkflowError:
        return None
    if not isinstance(payload, Mapping):
        return None
    raw_window_id = payload.get("id")
    if not isinstance(raw_window_id, int):
        return None
    return {**payload, "id": raw_window_id}


def _validate_workflow_space_or_none(
    yabai: YabaiClient,
    *,
    workflow_space,
    allowed_layouts: Iterable[str],
) -> bool:
    try:
        validate_workflow_space(
            yabai,
            workflow_space=workflow_space,
            allowed_layouts=allowed_layouts,
        )
    except WorkflowError:
        return False
    return True
