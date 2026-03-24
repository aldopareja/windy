from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .current_space import (
    derive_workflow_space_from_window,
    query_recent_window_record,
)
from .eligibility import is_eligible_window
from .errors import WorkflowError
from .models import EligibleWorkflowSpace, WorkflowSpaceState
from .state import WorkflowStateStore
from .yabai import YabaiClient


@dataclass(frozen=True)
class NewEligibleWindowPlacementResult:
    action: str
    visible_window_id: int
    background_window_ids: list[int]


def place_window_as_new_eligible(
    *,
    yabai: YabaiClient,
    state_store: WorkflowStateStore,
    workflow_space: EligibleWorkflowSpace,
    window_id: int,
    eligible_windows: Iterable[Mapping[str, Any]],
    persisted_space_state: WorkflowSpaceState,
    error_label: str,
) -> NewEligibleWindowPlacementResult:
    eligible_window_records = list(eligible_windows)
    eligible_window_ids = {int(window["id"]) for window in eligible_window_records}
    if window_id not in eligible_window_ids:
        raise WorkflowError(
            f"{error_label} is missing from the current eligible workflow window query "
            "for its workflow space."
        )

    if persisted_space_state.pending_split_direction is None:
        anchor_window_id = _resolve_anchor_window_id(
            yabai=yabai,
            workflow_space=workflow_space,
            window_id=window_id,
            eligible_windows=eligible_window_records,
            preferred_window_id=persisted_space_state.visible_window_id,
            background_window_ids=persisted_space_state.background_window_ids,
        )
        refreshed_background_window_ids = _refresh_background_window_ids(
            persisted_background_window_ids=persisted_space_state.background_window_ids,
            eligible_window_ids=eligible_window_ids,
            excluded_window_ids=(window_id, anchor_window_id),
        )
        prepared_state_payload = state_store.prepare_background_pool_payload(
            workflow_space=workflow_space,
            visible_window_id=window_id,
            background_window_ids=refreshed_background_window_ids,
            pending_split_direction=None,
        )
        yabai.stack_window(anchor_window_id, window_id)
        action = "stacked_on_focused_tile"
    else:
        refreshed_background_window_ids = _refresh_background_window_ids(
            persisted_background_window_ids=persisted_space_state.background_window_ids,
            eligible_window_ids=eligible_window_ids,
            excluded_window_ids=(window_id,),
        )
        prepared_state_payload = state_store.prepare_background_pool_payload(
            workflow_space=workflow_space,
            visible_window_id=window_id,
            background_window_ids=refreshed_background_window_ids,
            pending_split_direction=None,
        )
        action = "consumed_pending_split"

    state_store.write_payload(prepared_state_payload)
    return NewEligibleWindowPlacementResult(
        action=action,
        visible_window_id=window_id,
        background_window_ids=refreshed_background_window_ids,
    )


def _resolve_anchor_window_id(
    *,
    yabai: YabaiClient,
    workflow_space: EligibleWorkflowSpace,
    window_id: int,
    eligible_windows: Iterable[Mapping[str, Any]],
    preferred_window_id: int | None = None,
    background_window_ids: Iterable[int] = (),
) -> int:
    eligible_window_id_set = {int(window["id"]) for window in eligible_windows}
    focused_window_ids = [
        int(window["id"])
        for window in eligible_windows
        if bool(window.get("has-focus", False)) and int(window["id"]) != window_id
    ]
    if len(focused_window_ids) > 1:
        raise WorkflowError(
            "Expected at most one focused visible eligible workflow window in the target "
            "workflow space."
        )
    if focused_window_ids:
        return focused_window_ids[0]

    background_window_id_set = set(background_window_ids)
    preferred_anchor_is_valid = (
        preferred_window_id is not None
        and preferred_window_id != window_id
        and preferred_window_id in eligible_window_id_set
        and preferred_window_id not in background_window_id_set
    )

    try:
        recent_window = query_recent_window_record(
            yabai,
            description="most recently focused window",
        )
        recent_window_id = int(recent_window["id"])
        if recent_window_id == window_id:
            raise WorkflowError(
                "Failed to identify a previously focused visible eligible workflow window "
                "before the signaled window took focus."
            )
        recent_window_workflow_space = derive_workflow_space_from_window(
            recent_window,
            description="most recently focused window",
        )
        if recent_window_workflow_space != workflow_space:
            raise WorkflowError(
                "The most recently focused window does not belong to the signaled window's "
                "eligible workflow space."
            )
        if not is_eligible_window(
            recent_window,
            target_display=workflow_space.display,
            target_space=workflow_space.space,
        ):
            raise WorkflowError(
                "The most recently focused window is not an eligible workflow window in the "
                "signaled window's workflow space."
            )
        return recent_window_id
    except WorkflowError:
        if preferred_anchor_is_valid:
            return preferred_window_id
        raise


def _refresh_background_window_ids(
    *,
    persisted_background_window_ids: Iterable[int],
    eligible_window_ids: set[int],
    excluded_window_ids: Iterable[int],
) -> list[int]:
    excluded_window_id_set = set(excluded_window_ids)
    return [
        window_id
        for window_id in persisted_background_window_ids
        if window_id in eligible_window_ids and window_id not in excluded_window_id_set
    ]
