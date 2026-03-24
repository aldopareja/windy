from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .current_space import query_eligible_windows, validate_workflow_space
from .errors import WorkflowError
from .models import (
    TrackedVisibleWindowExitRecoveryResult,
    WorkflowSpaceState,
)
from .split import DEFAULT_PENDING_SPLIT_DIRECTION
from .state import WorkflowStateStore
from .yabai import YabaiClient

WINDOW_DESTROYED_EVENT = "window_destroyed"
SUPPORTED_TRACKED_VISIBLE_WINDOW_EXIT_EVENTS = frozenset(
    {WINDOW_DESTROYED_EVENT, "window_minimized", "window_moved"}
)


@dataclass(frozen=True)
class _EligibleWindowVisibilityState:
    eligible_window_ids: list[int]
    visible_window_ids: list[int]
    background_window_ids: list[int]


class TrackedVisibleWindowExitRecoveryService:
    def __init__(
        self,
        *,
        yabai: YabaiClient,
        state_store: WorkflowStateStore,
        window_id: int,
        event: str,
    ):
        self._yabai = yabai
        self._state_store = state_store
        self._window_id = window_id
        self._event = event

    def run(self) -> TrackedVisibleWindowExitRecoveryResult:
        normalized_event = _normalize_supported_visible_window_exit_event(self._event)
        matched_space_state = _resolve_matched_space_state(
            self._state_store.read_all_space_states(),
            window_id=self._window_id,
        )
        if matched_space_state is None:
            return TrackedVisibleWindowExitRecoveryResult(
                window_id=self._window_id,
                event=normalized_event,
                workflow_space=None,
                action="ignored_untracked_visible_window",
                visible_window_id=None,
                background_window_ids=[],
                pending_split_direction=None,
            )

        if matched_space_state.pending_split_direction is not None:
            return TrackedVisibleWindowExitRecoveryResult(
                window_id=self._window_id,
                event=normalized_event,
                workflow_space=matched_space_state.workflow_space,
                action="ignored_pending_split",
                visible_window_id=matched_space_state.visible_window_id,
                background_window_ids=list(matched_space_state.background_window_ids),
                pending_split_direction=matched_space_state.pending_split_direction,
            )

        validate_workflow_space(
            self._yabai,
            workflow_space=matched_space_state.workflow_space,
            allowed_layouts=("bsp",),
        )
        eligible_windows = query_eligible_windows(
            self._yabai,
            workflow_space=matched_space_state.workflow_space,
        )
        visibility_state = _resolve_eligible_window_visibility_state(eligible_windows)
        if self._window_id in visibility_state.eligible_window_ids:
            return TrackedVisibleWindowExitRecoveryResult(
                window_id=self._window_id,
                event=normalized_event,
                workflow_space=matched_space_state.workflow_space,
                action="ignored_still_eligible",
                visible_window_id=matched_space_state.visible_window_id,
                background_window_ids=list(matched_space_state.background_window_ids),
                pending_split_direction=matched_space_state.pending_split_direction,
            )

        refreshed_background_window_ids = _refresh_background_window_ids(
            persisted_background_window_ids=matched_space_state.background_window_ids,
            eligible_background_window_ids=visibility_state.background_window_ids,
        )
        visible_background_window_ids = _resolve_visible_background_window_ids(
            persisted_background_window_ids=matched_space_state.background_window_ids,
            visible_window_ids=visibility_state.visible_window_ids,
        )

        visible_background_candidate = (
            visible_background_window_ids[0] if visible_background_window_ids else None
        )
        if visible_background_candidate is not None:
            return self._commit_background_recovery(
                matched_space_state,
                candidate_window_id=visible_background_candidate,
                refreshed_background_window_ids=refreshed_background_window_ids,
                should_promote=False,
            )

        if refreshed_background_window_ids:
            return self._commit_background_recovery(
                matched_space_state,
                candidate_window_id=refreshed_background_window_ids[0],
                refreshed_background_window_ids=refreshed_background_window_ids,
                should_promote=True,
            )

        remaining_visible_window_ids = list(visibility_state.visible_window_ids)
        if remaining_visible_window_ids:
            next_visible_window_id = remaining_visible_window_ids[0]
            prepared_state_payload = self._state_store.prepare_background_pool_payload(
                workflow_space=matched_space_state.workflow_space,
                visible_window_id=next_visible_window_id,
                background_window_ids=[],
                pending_split_direction=None,
            )
            self._state_store.write_payload(prepared_state_payload)
            return TrackedVisibleWindowExitRecoveryResult(
                window_id=self._window_id,
                event=normalized_event,
                workflow_space=matched_space_state.workflow_space,
                action="retargeted_remaining_visible_window",
                visible_window_id=next_visible_window_id,
                background_window_ids=[],
                pending_split_direction=None,
            )

        prepared_state_payload = self._state_store.prepare_space_deletion_payload(
            matched_space_state.workflow_space
        )
        self._state_store.write_payload(prepared_state_payload)
        return TrackedVisibleWindowExitRecoveryResult(
            window_id=self._window_id,
            event=normalized_event,
            workflow_space=matched_space_state.workflow_space,
            action="removed_empty_space_state",
            visible_window_id=None,
            background_window_ids=[],
            pending_split_direction=None,
        )

    def _commit_background_recovery(
        self,
        space_state: WorkflowSpaceState,
        *,
        candidate_window_id: int,
        refreshed_background_window_ids: list[int],
        should_promote: bool,
    ) -> TrackedVisibleWindowExitRecoveryResult:
        remaining_background_window_ids = [
            window_id
            for window_id in refreshed_background_window_ids
            if window_id != candidate_window_id
        ]
        prepared_state_payload = self._state_store.prepare_background_pool_payload(
            workflow_space=space_state.workflow_space,
            visible_window_id=candidate_window_id,
            background_window_ids=remaining_background_window_ids,
            pending_split_direction=None,
        )
        if should_promote:
            self._yabai.promote_stacked_window(
                candidate_window_id,
                DEFAULT_PENDING_SPLIT_DIRECTION,
            )
        self._state_store.write_payload(prepared_state_payload)
        return TrackedVisibleWindowExitRecoveryResult(
            window_id=self._window_id,
            event=_normalize_supported_visible_window_exit_event(self._event),
            workflow_space=space_state.workflow_space,
            action="recovered_with_background_window",
            visible_window_id=candidate_window_id,
            background_window_ids=remaining_background_window_ids,
            pending_split_direction=None,
        )


def _resolve_matched_space_state(
    space_states: list[WorkflowSpaceState], *, window_id: int
) -> WorkflowSpaceState | None:
    matched_space_states = [
        space_state
        for space_state in space_states
        if space_state.visible_window_id == window_id
    ]
    if len(matched_space_states) > 1:
        raise WorkflowError(
            "Tracked visible window appears in more than one tracked workflow space."
        )
    if not matched_space_states:
        return None
    return matched_space_states[0]


def _refresh_background_window_ids(
    *,
    persisted_background_window_ids: Iterable[int],
    eligible_background_window_ids: Iterable[int],
) -> list[int]:
    eligible_background_window_id_set = set(eligible_background_window_ids)
    return [
        window_id
        for window_id in persisted_background_window_ids
        if window_id in eligible_background_window_id_set
    ]


def _resolve_visible_background_window_ids(
    *,
    persisted_background_window_ids: Iterable[int],
    visible_window_ids: Iterable[int],
) -> list[int]:
    visible_window_id_set = set(visible_window_ids)
    return [
        window_id
        for window_id in persisted_background_window_ids
        if window_id in visible_window_id_set
    ]


def _resolve_eligible_window_visibility_state(
    eligible_windows: Iterable[Mapping[str, Any]],
) -> _EligibleWindowVisibilityState:
    eligible_window_ids: list[int] = []
    visible_window_ids: list[int] = []
    background_window_ids: list[int] = []
    for window in eligible_windows:
        window_id = window.get("id")
        if not isinstance(window_id, int):
            raise WorkflowError(
                "Expected yabai to provide an integer 'id' for an eligible workflow window."
            )
        eligible_window_ids.append(window_id)
        stack_index = window.get("stack-index")
        if stack_index is None:
            visible_window_ids.append(window_id)
            continue
        if not isinstance(stack_index, int):
            raise WorkflowError(
                "Expected yabai to provide an integer 'stack-index' for eligible "
                f"workflow window {window_id}."
            )
        if stack_index <= 1:
            visible_window_ids.append(window_id)
            continue
        background_window_ids.append(window_id)
    return _EligibleWindowVisibilityState(
        eligible_window_ids=eligible_window_ids,
        visible_window_ids=visible_window_ids,
        background_window_ids=background_window_ids,
    )


def _normalize_supported_visible_window_exit_event(raw_event: str) -> str:
    candidate = raw_event.strip().lower()
    if candidate not in SUPPORTED_TRACKED_VISIBLE_WINDOW_EXIT_EVENTS:
        raise WorkflowError(
            "tracked_visible_window_exit_recovery received an unsupported event: "
            f"{raw_event!r}"
        )
    return candidate
