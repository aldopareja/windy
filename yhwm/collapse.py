from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping

from .eligibility import is_eligible_window
from .errors import WorkflowError
from .models import CollapseResult, EligibleWorkflowSpace
from .state import WorkflowStateStore
from .yabai import YabaiClient


class CollapseCurrentSpaceService:
    def __init__(self, yabai: YabaiClient, state_store: WorkflowStateStore):
        self._yabai = yabai
        self._state_store = state_store

    def run(self) -> CollapseResult:
        focused_window = self._require_mapping(
            self._yabai.query_focused_window(),
            "focused window query",
        )
        workflow_space = EligibleWorkflowSpace(
            display=self._require_int(focused_window, "display", "focused window"),
            space=self._require_int(focused_window, "space", "focused window"),
        )

        target_display = self._require_mapping(
            self._yabai.query_display(workflow_space.display),
            "display query",
        )
        target_space = self._require_mapping(
            self._yabai.query_space(workflow_space.space),
            "space query",
        )

        self._validate_target_space(
            workflow_space=workflow_space,
            display_record=target_display,
            space_record=target_space,
        )
        self._validate_environment(workflow_space.space)

        windows = self._yabai.query_windows_for_space(workflow_space.space)
        eligible_windows = self._filter_eligible_windows(
            windows,
            workflow_space=workflow_space,
        )

        focused_window_id = self._require_int(focused_window, "id", "focused window")
        eligible_window_ids = [window["id"] for window in eligible_windows]
        if focused_window_id not in eligible_window_ids:
            raise WorkflowError(
                "Focused window is not an eligible workflow window in the current "
                "eligible workflow space."
            )

        background_window_ids = [
            window_id for window_id in eligible_window_ids if window_id != focused_window_id
        ]

        self._yabai.set_space_layout(workflow_space.space, "bsp")
        for background_window_id in background_window_ids:
            self._yabai.stack_window(focused_window_id, background_window_id)
        self._yabai.focus_window(focused_window_id)

        result = CollapseResult(
            workflow_space=workflow_space,
            focused_window_id=focused_window_id,
            background_window_ids=background_window_ids,
        )
        self._state_store.record_collapse(result)
        return result

    def _filter_eligible_windows(
        self,
        windows: Iterable[Any],
        *,
        workflow_space: EligibleWorkflowSpace,
    ) -> List[Dict[str, Any]]:
        eligible_windows: List[Dict[str, Any]] = []
        for window in windows:
            record = self._require_mapping(window, "space window query")
            if is_eligible_window(
                record,
                target_display=workflow_space.display,
                target_space=workflow_space.space,
            ):
                record_id = self._require_int(record, "id", "space window")
                eligible_windows.append({**record, "id": record_id})
        return eligible_windows

    def _validate_environment(self, space: int) -> None:
        focus_follows_mouse = self._yabai.get_config("focus_follows_mouse")
        if focus_follows_mouse != "off":
            raise WorkflowError(
                "Incompatible yabai environment: focus_follows_mouse must be 'off' "
                f"but is '{focus_follows_mouse}'."
            )

        mouse_follows_focus = self._yabai.get_config("mouse_follows_focus")
        if mouse_follows_focus != "off":
            raise WorkflowError(
                "Incompatible yabai environment: mouse_follows_focus must be 'off' "
                f"but is '{mouse_follows_focus}'."
            )

        layout = self._yabai.get_config("layout", space=space)
        if layout not in {"bsp", "stack", "float"}:
            raise WorkflowError(
                "Incompatible yabai environment: expected a supported space layout "
                f"for space {space}, got '{layout}'."
            )

    def _validate_target_space(
        self,
        *,
        workflow_space: EligibleWorkflowSpace,
        display_record: Mapping[str, Any],
        space_record: Mapping[str, Any],
    ) -> None:
        display_index = self._require_int(display_record, "index", "display")
        if display_index != workflow_space.display:
            raise WorkflowError(
                "Focused window display does not match the resolved yabai display."
            )

        space_index = self._require_int(space_record, "index", "space")
        if space_index != workflow_space.space:
            raise WorkflowError(
                "Focused window space does not match the resolved yabai space."
            )

        space_display = self._require_int(space_record, "display", "space")
        if space_display != workflow_space.display:
            raise WorkflowError(
                "Target space does not belong to the focused window display."
            )

        if not bool(space_record.get("is-visible", False)):
            raise WorkflowError("Target space is not visible and is not eligible for collapse.")

        if bool(space_record.get("is-native-fullscreen", False)):
            raise WorkflowError(
                "Target space is native fullscreen and is not eligible for collapse."
            )

    @staticmethod
    def _require_mapping(value: Any, description: str) -> Dict[str, Any]:
        if not isinstance(value, Mapping):
            raise WorkflowError(f"Expected yabai to return an object for {description}.")
        return dict(value)

    @staticmethod
    def _require_int(record: Mapping[str, Any], key: str, description: str) -> int:
        value = record.get(key)
        if not isinstance(value, int):
            raise WorkflowError(
                f"Expected yabai to provide an integer '{key}' for {description}."
            )
        return value
