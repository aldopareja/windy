from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Collection, Dict, Iterable, List, Mapping

from .eligibility import is_eligible_window
from .errors import WorkflowError
from .models import EligibleWorkflowSpace
from .yabai import YabaiClient


@dataclass(frozen=True)
class CurrentSpaceTarget:
    workflow_space: EligibleWorkflowSpace
    focused_window_id: int


def query_window_record(
    yabai: YabaiClient, *, window_id: int, description: str
) -> Dict[str, Any]:
    window = _require_mapping(
        yabai.query_window(window_id),
        f"{description} query",
    )
    record_window_id = _require_int(window, "id", description)
    if record_window_id != window_id:
        raise WorkflowError(
            f"Expected yabai to return window {window_id} for {description}."
        )
    return {**window, "id": record_window_id}


def query_recent_window_record(yabai: YabaiClient, *, description: str) -> Dict[str, Any]:
    window = _require_mapping(
        yabai.query_recent_window(),
        f"{description} query",
    )
    record_window_id = _require_int(window, "id", description)
    return {**window, "id": record_window_id}


def derive_workflow_space_from_window(
    window: Mapping[str, Any], *, description: str
) -> EligibleWorkflowSpace:
    return EligibleWorkflowSpace(
        display=_require_int(window, "display", description),
        space=_require_int(window, "space", description),
    )


def validate_workflow_space(
    yabai: YabaiClient,
    *,
    workflow_space: EligibleWorkflowSpace,
    allowed_layouts: Collection[str],
) -> None:
    target_display = _require_mapping(
        yabai.query_display(workflow_space.display),
        "display query",
    )
    target_space = _require_mapping(
        yabai.query_space(workflow_space.space),
        "space query",
    )

    _validate_target_space(
        workflow_space=workflow_space,
        display_record=target_display,
        space_record=target_space,
    )
    _validate_environment(yabai, workflow_space.space, allowed_layouts=allowed_layouts)


def resolve_current_space_target(
    yabai: YabaiClient, *, allowed_layouts: Collection[str]
) -> CurrentSpaceTarget:
    focused_window = _require_mapping(
        yabai.query_focused_window(),
        "focused window query",
    )
    workflow_space = derive_workflow_space_from_window(
        focused_window,
        description="focused window",
    )
    validate_workflow_space(
        yabai,
        workflow_space=workflow_space,
        allowed_layouts=allowed_layouts,
    )

    focused_window_id = _require_int(focused_window, "id", "focused window")
    if not is_eligible_window(
        focused_window,
        target_display=workflow_space.display,
        target_space=workflow_space.space,
    ):
        raise WorkflowError(
            "Focused window is not an eligible workflow window in the current "
            "eligible workflow space."
        )

    return CurrentSpaceTarget(
        workflow_space=workflow_space,
        focused_window_id=focused_window_id,
    )


def query_eligible_windows(
    yabai: YabaiClient,
    *,
    workflow_space: EligibleWorkflowSpace,
) -> List[Dict[str, Any]]:
    windows = yabai.query_windows_for_space(workflow_space.space)
    eligible_windows: List[Dict[str, Any]] = []
    for window in windows:
        record = _require_mapping(window, "space window query")
        if is_eligible_window(
            record,
            target_display=workflow_space.display,
            target_space=workflow_space.space,
        ):
            record_id = _require_int(record, "id", "space window")
            eligible_windows.append({**record, "id": record_id})
    return eligible_windows


def require_focused_window_in_eligible_windows(
    focused_window_id: int, eligible_windows: Iterable[Mapping[str, Any]]
) -> None:
    eligible_window_ids = []
    for window in eligible_windows:
        eligible_window_ids.append(_require_int(window, "id", "eligible workflow window"))

    if focused_window_id not in eligible_window_ids:
        raise WorkflowError(
            "Focused window is not an eligible workflow window in the current "
            "eligible workflow space."
        )


def _validate_environment(
    yabai: YabaiClient, space: int, *, allowed_layouts: Collection[str]
) -> None:
    focus_follows_mouse = _normalize_focus_follows_mouse(
        yabai.get_config("focus_follows_mouse")
    )
    if focus_follows_mouse != "off":
        raise WorkflowError(
            "Incompatible yabai environment: focus_follows_mouse must be 'off' "
            f"but is '{focus_follows_mouse}'."
        )

    mouse_follows_focus = _normalize_off_config(yabai.get_config("mouse_follows_focus"))
    if mouse_follows_focus != "off":
        raise WorkflowError(
            "Incompatible yabai environment: mouse_follows_focus must be 'off' "
            f"but is '{mouse_follows_focus}'."
        )

    layout = _normalize_config_value(yabai.get_config("layout", space=space))
    normalized_allowed_layouts = {layout_name.strip().lower() for layout_name in allowed_layouts}
    if layout not in normalized_allowed_layouts:
        raise WorkflowError(
            "Incompatible yabai environment: "
            f"space {space} must use {_format_allowed_layouts(normalized_allowed_layouts)} "
            f"but is '{layout}'."
        )


def _validate_target_space(
    *,
    workflow_space: EligibleWorkflowSpace,
    display_record: Mapping[str, Any],
    space_record: Mapping[str, Any],
) -> None:
    display_index = _require_int(display_record, "index", "display")
    if display_index != workflow_space.display:
        raise WorkflowError(
            "Focused window display does not match the resolved yabai display."
        )

    space_index = _require_int(space_record, "index", "space")
    if space_index != workflow_space.space:
        raise WorkflowError("Focused window space does not match the resolved yabai space.")

    space_display = _require_int(space_record, "display", "space")
    if space_display != workflow_space.display:
        raise WorkflowError("Target space does not belong to the focused window display.")

    if not bool(space_record.get("is-visible", False)):
        raise WorkflowError("Target space is not visible and is not an eligible workflow space.")

    if bool(space_record.get("is-native-fullscreen", False)):
        raise WorkflowError(
            "Target space is native fullscreen and is not an eligible workflow space."
        )


def _format_allowed_layouts(allowed_layouts: Collection[str]) -> str:
    ordered_layouts = sorted(allowed_layouts)
    if len(ordered_layouts) == 1:
        return f"layout '{ordered_layouts[0]}'"
    return "one of " + ", ".join(f"'{layout}'" for layout in ordered_layouts)


def _require_mapping(value: Any, description: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowError(f"Expected yabai to return an object for {description}.")
    return dict(value)


def _require_int(record: Mapping[str, Any], key: str, description: str) -> int:
    value = record.get(key)
    if not isinstance(value, int):
        raise WorkflowError(
            f"Expected yabai to provide an integer '{key}' for {description}."
        )
    return value


def _normalize_focus_follows_mouse(value: Any) -> str:
    normalized = _normalize_config_value(value)
    if normalized == "disabled":
        return "off"
    return normalized


def _normalize_off_config(value: Any) -> str:
    normalized = _normalize_config_value(value)
    if normalized in {"disabled", "false", "0"}:
        return "off"
    return normalized


def _normalize_config_value(value: Any) -> str:
    if not isinstance(value, str):
        raise WorkflowError("Expected yabai config queries to return text values.")
    return value.strip().lower()
