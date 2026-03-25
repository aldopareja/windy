from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable, Mapping, Optional

from .current_space import (
    derive_workflow_space_from_window,
    query_eligible_windows,
    query_focused_window_record,
    query_recent_window_record,
    query_window_record,
    require_focused_window_in_eligible_windows,
    resolve_current_space_target,
    validate_workflow_space,
)
from .eligibility import is_eligible_window
from .errors import WorkflowError
from .models import AltTabSession, FocusGuard, RuntimeState, TrackedSpaceState
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
        if not _validate_float_space_or_none(self._yabai, workflow_space=workflow_space):
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
        self._state_store.write(
            _clear_space_runtime_state(
                _delete_space_state(state, workflow_space.storage_key),
                workflow_space=workflow_space,
            )
        )

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
        require_focused_window_in_eligible_windows(
            target.focused_window_id,
            eligible_windows,
        )

        background_window_ids = [
            int(window["id"])
            for window in eligible_windows
            if int(window["id"]) != target.focused_window_id
        ]

        self._yabai.set_space_layout(target.workflow_space.space, "bsp")
        for window_id in background_window_ids:
            self._yabai.stack_window(target.focused_window_id, window_id)
        self._yabai.focus_window(target.focused_window_id)

        next_spaces = dict(state.spaces)
        next_spaces[target.workflow_space.storage_key] = TrackedSpaceState(
            workflow_space=target.workflow_space,
            leader_window_id=target.focused_window_id,
            background_window_ids=background_window_ids,
            pending_split_direction=None,
        )
        self._state_store.write(
            RuntimeState(
                spaces=next_spaces,
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
        tracked = state.spaces.get(target.workflow_space.storage_key)
        if tracked is None:
            raise WorkflowError("Current space is not tracked. Run `yhwm reseed` first.")

        eligible_windows = query_eligible_windows(
            self._yabai,
            workflow_space=target.workflow_space,
        )
        require_focused_window_in_eligible_windows(
            target.focused_window_id,
            eligible_windows,
        )
        eligible_window_ids = {int(window["id"]) for window in eligible_windows}
        background_window_ids = _filter_window_ids(
            tracked.background_window_ids,
            allowed_window_ids=eligible_window_ids,
            excluded_window_ids=(target.focused_window_id,),
        )

        if background_window_ids:
            promoted_window_id = background_window_ids[0]
            remaining_background = background_window_ids[1:]
            self._yabai.promote_stacked_window(promoted_window_id, normalized_direction)
            self._yabai.focus_window(target.focused_window_id)
            next_tracked = TrackedSpaceState(
                workflow_space=target.workflow_space,
                leader_window_id=target.focused_window_id,
                background_window_ids=remaining_background,
                pending_split_direction=None,
            )
        else:
            self._yabai.arm_window_split(target.focused_window_id, normalized_direction)
            next_tracked = TrackedSpaceState(
                workflow_space=target.workflow_space,
                leader_window_id=target.focused_window_id,
                background_window_ids=[],
                pending_split_direction=normalized_direction,
            )

        next_state = _replace_space_state(state, next_tracked)
        self._state_store.write(next_state)

    def handle_focus(self, window_id: int) -> None:
        state = self._state_store.read()
        if state.alttab_session is not None:
            return

        focused_window = query_window_record(
            self._yabai,
            window_id=window_id,
            description=f"focused window {window_id}",
        )
        workflow_space = derive_workflow_space_from_window(
            focused_window,
            description=f"focused window {window_id}",
        )
        if state.focus_guard is not None and _focus_guard_matches(
            state.focus_guard,
            workflow_space=workflow_space,
            window_id=window_id,
        ):
            self._state_store.write(replace(state, focus_guard=None))
            return

        if not is_eligible_window(
            focused_window,
            target_display=workflow_space.display,
            target_space=workflow_space.space,
        ):
            return

        tracked = state.spaces.get(workflow_space.storage_key)
        if tracked is None:
            return

        eligible_windows = query_eligible_windows(
            self._yabai,
            workflow_space=workflow_space,
        )
        eligible_window_ids = {int(window["id"]) for window in eligible_windows}

        if window_id in tracked.background_window_ids:
            next_background = _filter_window_ids(
                tracked.background_window_ids,
                allowed_window_ids=eligible_window_ids,
                excluded_window_ids=(window_id,),
            )
            if tracked.leader_window_id in eligible_window_ids and tracked.leader_window_id != window_id:
                next_background.append(tracked.leader_window_id)
        else:
            next_background = _filter_window_ids(
                tracked.background_window_ids,
                allowed_window_ids=eligible_window_ids,
                excluded_window_ids=(window_id,),
            )
            for background_window_id in next_background:
                self._yabai.stack_window(window_id, background_window_id)

        next_tracked = TrackedSpaceState(
            workflow_space=workflow_space,
            leader_window_id=window_id,
            background_window_ids=next_background,
            pending_split_direction=tracked.pending_split_direction,
        )
        self._state_store.write(
            _replace_space_state(
                replace(state, focus_guard=None),
                next_tracked,
            )
        )

    def handle_window_event(self, *, event: str, window_id: int) -> None:
        if event not in SUPPORTED_SIGNAL_WINDOW_EVENTS:
            raise WorkflowError(f"Unsupported window event: {event}")

        if event in ARRIVAL_EVENTS:
            self._handle_arrival(window_id)
            return
        if event in LOSS_EVENTS:
            self._handle_loss(window_id, require_current_absence=True)
            return
        if event == "window_moved":
            self._handle_loss(window_id, require_current_absence=False)
            return

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

        tracked = state.spaces.get(workflow_space.storage_key)
        if tracked is None or tracked.leader_window_id != focused_window_id:
            return

        validate_workflow_space(
            self._yabai,
            workflow_space=workflow_space,
            allowed_layouts=("bsp",),
        )
        self._state_store.write(
            replace(
                state,
                alttab_session=AltTabSession(
                    origin_window_id=focused_window_id,
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
            try:
                selected_window_id = int(
                    query_focused_window_record(
                        self._yabai,
                        description="focused window",
                    )["id"]
                )
            except WorkflowError:
                selected_window_id = None

        cleared_state = replace(state, alttab_session=None, focus_guard=None)
        if selected_window_id is None or selected_window_id == session.origin_window_id:
            self._state_store.write(cleared_state)
            return

        tracked = cleared_state.spaces.get(session.origin_workflow_space.storage_key)
        if tracked is None:
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

        eligible_window_ids = {
            int(window["id"])
            for window in query_eligible_windows(
                self._yabai,
                workflow_space=session.origin_workflow_space,
            )
        }
        background_window_ids = _filter_window_ids(
            tracked.background_window_ids,
            allowed_window_ids=eligible_window_ids,
            excluded_window_ids=(selected_window_id,),
        )

        if selected_window_id in tracked.background_window_ids:
            self._yabai.focus_window(selected_window_id)
            if session.origin_window_id in eligible_window_ids and session.origin_window_id != selected_window_id:
                background_window_ids.append(session.origin_window_id)
            next_tracked = TrackedSpaceState(
                workflow_space=session.origin_workflow_space,
                leader_window_id=selected_window_id,
                background_window_ids=background_window_ids,
                pending_split_direction=tracked.pending_split_direction,
            )
            self._state_store.write(_replace_space_state(cleared_state, next_tracked))
            return

        self._yabai.swap_window(session.origin_window_id, selected_window_id)
        self._yabai.focus_window(selected_window_id)
        for background_window_id in background_window_ids:
            self._yabai.stack_window(selected_window_id, background_window_id)
        next_tracked = TrackedSpaceState(
            workflow_space=session.origin_workflow_space,
            leader_window_id=selected_window_id,
            background_window_ids=background_window_ids,
            pending_split_direction=tracked.pending_split_direction,
        )
        self._state_store.write(_replace_space_state(cleared_state, next_tracked))

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

    def _handle_arrival(self, window_id: int) -> None:
        state = self._state_store.read()
        created_window = _query_window_record_or_none(self._yabai, window_id)
        if created_window is None:
            return

        workflow_space = derive_workflow_space_from_window(
            created_window,
            description=f"arrival window {window_id}",
        )
        if not is_eligible_window(
            created_window,
            target_display=workflow_space.display,
            target_space=workflow_space.space,
        ):
            return

        tracked = state.spaces.get(workflow_space.storage_key)
        if tracked is None:
            return

        eligible_windows = query_eligible_windows(
            self._yabai,
            workflow_space=workflow_space,
        )
        eligible_window_ids = {int(window["id"]) for window in eligible_windows}
        background_window_ids = _filter_window_ids(
            tracked.background_window_ids,
            allowed_window_ids=eligible_window_ids,
            excluded_window_ids=(window_id,),
        )

        if tracked.pending_split_direction is not None:
            next_tracked = TrackedSpaceState(
                workflow_space=workflow_space,
                leader_window_id=tracked.leader_window_id,
                background_window_ids=background_window_ids,
                pending_split_direction=None,
            )
            self._state_store.write(_replace_space_state(state, next_tracked))
            return

        anchor_window_id = self._resolve_arrival_anchor(
            workflow_space=workflow_space,
            created_window_id=window_id,
            eligible_windows=eligible_windows,
            tracked=tracked,
        )
        if anchor_window_id is None:
            next_tracked = TrackedSpaceState(
                workflow_space=workflow_space,
                leader_window_id=window_id,
                background_window_ids=background_window_ids,
                pending_split_direction=None,
            )
            self._state_store.write(_replace_space_state(state, next_tracked))
            return

        self._yabai.stack_window(anchor_window_id, window_id)
        self._yabai.focus_window(window_id)
        for background_window_id in background_window_ids:
            self._yabai.stack_window(window_id, background_window_id)

        next_background = list(background_window_ids)
        if anchor_window_id != window_id and anchor_window_id not in next_background:
            next_background.append(anchor_window_id)

        next_tracked = TrackedSpaceState(
            workflow_space=workflow_space,
            leader_window_id=window_id,
            background_window_ids=next_background,
            pending_split_direction=None,
        )
        self._state_store.write(_replace_space_state(state, next_tracked))

    def _handle_loss(self, window_id: int, *, require_current_absence: bool) -> None:
        state = self._state_store.read()
        tracked = _find_tracked_space_for_window(state, window_id)
        if tracked is None:
            return

        current_window = _query_window_record_or_none(self._yabai, window_id)
        if current_window is not None and not require_current_absence:
            if is_eligible_window(
                current_window,
                target_display=tracked.workflow_space.display,
                target_space=tracked.workflow_space.space,
            ):
                return

        if window_id in tracked.background_window_ids:
            next_background = [
                candidate
                for candidate in tracked.background_window_ids
                if candidate != window_id
            ]
            next_tracked = TrackedSpaceState(
                workflow_space=tracked.workflow_space,
                leader_window_id=tracked.leader_window_id,
                background_window_ids=next_background,
                pending_split_direction=tracked.pending_split_direction,
            )
            self._state_store.write(_replace_space_state(state, next_tracked))
            return

        eligible_windows = query_eligible_windows(
            self._yabai,
            workflow_space=tracked.workflow_space,
        )
        if not eligible_windows:
            self._state_store.write(_delete_space_state(state, tracked.workflow_space.storage_key))
            return

        next_leader_window_id = _choose_replacement_leader(
            eligible_windows=eligible_windows,
            background_window_ids=tracked.background_window_ids,
        )
        if next_leader_window_id is None:
            self._state_store.write(_delete_space_state(state, tracked.workflow_space.storage_key))
            return

        eligible_window_ids = {int(window["id"]) for window in eligible_windows}
        next_background = _filter_window_ids(
            tracked.background_window_ids,
            allowed_window_ids=eligible_window_ids,
            excluded_window_ids=(next_leader_window_id,),
        )

        if next_leader_window_id in tracked.background_window_ids:
            self._yabai.focus_window(next_leader_window_id)
        else:
            for background_window_id in next_background:
                self._yabai.stack_window(next_leader_window_id, background_window_id)

        next_tracked = TrackedSpaceState(
            workflow_space=tracked.workflow_space,
            leader_window_id=next_leader_window_id,
            background_window_ids=next_background,
            pending_split_direction=None,
        )
        self._state_store.write(_replace_space_state(state, next_tracked))

    def _resolve_arrival_anchor(
        self,
        *,
        workflow_space,
        created_window_id: int,
        eligible_windows: Iterable[Mapping[str, Any]],
        tracked: TrackedSpaceState,
    ) -> Optional[int]:
        eligible_window_records = list(eligible_windows)
        eligible_window_ids = {int(window["id"]) for window in eligible_window_records}
        focused_candidates = [
            int(window["id"])
            for window in eligible_window_records
            if bool(window.get("has-focus", False)) and int(window["id"]) != created_window_id
        ]
        if focused_candidates:
            return focused_candidates[0]

        recent_window = _query_recent_window_or_none(self._yabai)
        if recent_window is not None:
            recent_window_id = int(recent_window["id"])
            recent_workflow_space = derive_workflow_space_from_window(
                recent_window,
                description="most recently focused window",
            )
            if (
                recent_workflow_space == workflow_space
                and recent_window_id != created_window_id
                and recent_window_id in eligible_window_ids
                and recent_window_id not in tracked.background_window_ids
            ):
                return recent_window_id

        if (
            tracked.leader_window_id in eligible_window_ids
            and tracked.leader_window_id != created_window_id
            and tracked.leader_window_id not in tracked.background_window_ids
        ):
            return tracked.leader_window_id

        for window in eligible_window_records:
            candidate_window_id = int(window["id"])
            if (
                candidate_window_id != created_window_id
                and candidate_window_id not in tracked.background_window_ids
            ):
                return candidate_window_id
        return None


def _replace_space_state(state: RuntimeState, tracked: TrackedSpaceState) -> RuntimeState:
    next_spaces = dict(state.spaces)
    next_spaces[tracked.workflow_space.storage_key] = tracked
    return replace(state, spaces=next_spaces)


def _delete_space_state(state: RuntimeState, storage_key: str) -> RuntimeState:
    next_spaces = dict(state.spaces)
    next_spaces.pop(storage_key, None)
    return replace(state, spaces=next_spaces)


def _clear_space_runtime_state(
    state: RuntimeState,
    *,
    workflow_space,
) -> RuntimeState:
    next_state = state
    if (
        next_state.alttab_session is not None
        and next_state.alttab_session.origin_workflow_space == workflow_space
    ):
        next_state = replace(next_state, alttab_session=None)
    if next_state.focus_guard is not None and next_state.focus_guard.workflow_space == workflow_space:
        next_state = replace(next_state, focus_guard=None)
    return next_state


def _filter_window_ids(
    window_ids: Iterable[int],
    *,
    allowed_window_ids: set[int],
    excluded_window_ids: Iterable[int] = (),
) -> list[int]:
    excluded = set(excluded_window_ids)
    filtered: list[int] = []
    for window_id in window_ids:
        if window_id not in allowed_window_ids or window_id in excluded:
            continue
        if window_id not in filtered:
            filtered.append(window_id)
    return filtered


def _find_tracked_space_for_window(state: RuntimeState, window_id: int) -> Optional[TrackedSpaceState]:
    for tracked in state.spaces.values():
        if tracked.leader_window_id == window_id or window_id in tracked.background_window_ids:
            return tracked
    return None


def _choose_replacement_leader(
    *,
    eligible_windows: Iterable[Mapping[str, Any]],
    background_window_ids: Iterable[int],
) -> Optional[int]:
    eligible_window_records = list(eligible_windows)
    background_window_id_set = set(background_window_ids)
    for window in eligible_window_records:
        if bool(window.get("has-focus", False)):
            return int(window["id"])
    for window in eligible_window_records:
        candidate_window_id = int(window["id"])
        if candidate_window_id not in background_window_id_set:
            return candidate_window_id
    for window_id in background_window_ids:
        for window in eligible_window_records:
            if int(window["id"]) == window_id:
                return window_id
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


def _query_window_record_or_none(yabai: YabaiClient, window_id: int) -> Optional[dict[str, Any]]:
    try:
        return query_window_record(
            yabai,
            window_id=window_id,
            description=f"window {window_id}",
        )
    except WorkflowError:
        return None


def _query_recent_window_or_none(yabai: YabaiClient) -> Optional[dict[str, Any]]:
    try:
        return query_recent_window_record(
            yabai,
            description="most recently focused window",
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


def _validate_float_space_or_none(yabai: YabaiClient, *, workflow_space) -> bool:
    try:
        validate_workflow_space(
            yabai,
            workflow_space=workflow_space,
            allowed_layouts=("bsp", "stack", "float"),
        )
    except WorkflowError:
        return False
    return True
