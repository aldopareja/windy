from __future__ import annotations

from typing import Any, Iterable, Mapping

from .current_space import (
    derive_workflow_space_from_window,
    query_eligible_windows,
    query_window_record,
    validate_workflow_space,
)
from .eligibility import is_eligible_window
from .errors import WorkflowError
from .models import WindowCreatedResult
from .state import WorkflowStateStore
from .yabai import YabaiClient


class WindowCreatedService:
    def __init__(
        self,
        *,
        yabai: YabaiClient,
        state_store: WorkflowStateStore,
        window_id: int,
    ):
        self._yabai = yabai
        self._state_store = state_store
        self._window_id = window_id

    def run(self) -> WindowCreatedResult:
        created_window = query_window_record(
            self._yabai,
            window_id=self._window_id,
            description=f"created window {self._window_id}",
        )
        workflow_space = derive_workflow_space_from_window(
            created_window,
            description=f"created window {self._window_id}",
        )
        if not is_eligible_window(
            created_window,
            target_display=workflow_space.display,
            target_space=workflow_space.space,
        ):
            return WindowCreatedResult(
                created_window_id=self._window_id,
                workflow_space=workflow_space,
                action="ignored_ineligible",
                visible_window_id=None,
                background_window_ids=[],
            )

        persisted_space_state = self._state_store.read_space_state(workflow_space)
        if persisted_space_state is None:
            return WindowCreatedResult(
                created_window_id=self._window_id,
                workflow_space=workflow_space,
                action="ignored_untracked_space",
                visible_window_id=None,
                background_window_ids=[],
            )

        validate_workflow_space(
            self._yabai,
            workflow_space=workflow_space,
            allowed_layouts=("bsp",),
        )
        eligible_windows = query_eligible_windows(
            self._yabai,
            workflow_space=workflow_space,
        )
        eligible_window_ids = {window["id"] for window in eligible_windows}
        if self._window_id not in eligible_window_ids:
            raise WorkflowError(
                "Created eligible window is missing from the current eligible "
                "workflow window query for its workflow space."
            )

        anchor_window_id = _resolve_anchor_window_id(
            created_window_id=self._window_id,
            eligible_windows=eligible_windows,
            persisted_visible_window_id=persisted_space_state.visible_window_id,
        )
        refreshed_background_window_ids = _refresh_background_window_ids(
            persisted_background_window_ids=persisted_space_state.background_window_ids,
            eligible_window_ids=eligible_window_ids,
            excluded_window_ids=(self._window_id, anchor_window_id),
        )
        visible_window_id = _resolve_visible_window_id(
            created_window=created_window,
            fallback_window_id=anchor_window_id,
        )
        prepared_state_payload = self._state_store.prepare_background_pool_payload(
            workflow_space=workflow_space,
            visible_window_id=visible_window_id,
            background_window_ids=refreshed_background_window_ids,
            pending_split_direction=None,
        )

        if persisted_space_state.pending_split_direction is None:
            self._yabai.stack_window(anchor_window_id, self._window_id)
            action = "stacked_on_focused_tile"
        else:
            action = "consumed_pending_split"

        self._state_store.write_payload(prepared_state_payload)
        return WindowCreatedResult(
            created_window_id=self._window_id,
            workflow_space=workflow_space,
            action=action,
            visible_window_id=visible_window_id,
            background_window_ids=refreshed_background_window_ids,
        )


def _resolve_anchor_window_id(
    *,
    created_window_id: int,
    eligible_windows: Iterable[Mapping[str, Any]],
    persisted_visible_window_id: int,
) -> int:
    focused_window_ids = [
        int(window["id"])
        for window in eligible_windows
        if bool(window.get("has-focus", False)) and int(window["id"]) != created_window_id
    ]
    if len(focused_window_ids) > 1:
        raise WorkflowError(
            "Expected at most one focused visible eligible workflow window in the target "
            "workflow space."
        )
    if focused_window_ids:
        return focused_window_ids[0]

    eligible_window_ids = {int(window["id"]) for window in eligible_windows}
    if (
        persisted_visible_window_id != created_window_id
        and persisted_visible_window_id in eligible_window_ids
    ):
        return persisted_visible_window_id

    raise WorkflowError(
        "Failed to identify the focused visible eligible workflow window in the "
        "target workflow space."
    )


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


def _resolve_visible_window_id(
    *, created_window: Mapping[str, Any], fallback_window_id: int
) -> int:
    created_window_id = created_window.get("id")
    if isinstance(created_window_id, int) and bool(created_window.get("has-focus", False)):
        return created_window_id
    return fallback_window_id
