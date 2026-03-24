from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Callable, List, Optional

from .background_window_exit_cleanup import (
    BackgroundWindowExitCleanupService,
    SUPPORTED_BACKGROUND_WINDOW_EXIT_EVENTS,
)
from .background_window_return_as_new import (
    BackgroundWindowReturnAsNewService,
    SUPPORTED_BACKGROUND_WINDOW_RETURN_EVENTS,
)
from .collapse import CollapseCurrentSpaceService
from .errors import WorkflowError
from .state import WorkflowStateStore
from .split import SplitFromBackgroundPoolService
from .window_created import WindowCreatedService
from .window_focused import WindowFocusedService
from .yabai import SubprocessYabaiClient


def main(argv: Optional[List[str]] = None) -> int:
    return collapse_main(argv)


def collapse_main(argv: Optional[List[str]] = None) -> int:
    return _run_service_command(
        argv=argv,
        prog="collapse_current_space",
        description=(
            "Collapse the current eligible workflow space so the focused eligible "
            "window stays visible and other eligible windows become background "
            "stack members."
        ),
        service_factory=CollapseCurrentSpaceService,
    )


def split_main(argv: Optional[List[str]] = None) -> int:
    return _run_service_command(
        argv=argv,
        prog="split_from_background_pool",
        description=(
            "Split the focused eligible workflow tile by promoting one eligible "
            "background window from the same workflow space when available, or "
            "leave a native pending split when none are available."
        ),
        service_factory=SplitFromBackgroundPoolService,
    )


def window_created_main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser(
        prog="window_created",
        description=(
            "Handle one yabai window_created signal for tracked workflow spaces, "
            "either stacking the new eligible window onto the focused visible tile "
            "or consuming one pending native split."
        ),
    )
    parser.add_argument(
        "--window-id",
        default=os.environ.get("YABAI_WINDOW_ID"),
        help="Created yabai window id. Defaults to the YABAI_WINDOW_ID environment variable.",
    )
    args = parser.parse_args(argv)

    try:
        window_id = _parse_signal_window_id(
            command_name="window_created",
            raw_value=args.window_id,
        )
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    state_store = WorkflowStateStore(Path(args.state_file))
    yabai = SubprocessYabaiClient(args.yabai_bin)
    service = WindowCreatedService(
        yabai=yabai,
        state_store=state_store,
        window_id=window_id,
    )

    try:
        service.run()
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


def window_focused_main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser(
        prog="window_focused",
        description=(
            "Handle one yabai window_focused signal for tracked workflow spaces, "
            "updating only the tracked focused visible tile when the focused "
            "window is a visible eligible workflow window."
        ),
    )
    parser.add_argument(
        "--window-id",
        default=os.environ.get("YABAI_WINDOW_ID"),
        help="Focused yabai window id. Defaults to the YABAI_WINDOW_ID environment variable.",
    )
    args = parser.parse_args(argv)

    try:
        window_id = _parse_signal_window_id(
            command_name="window_focused",
            raw_value=args.window_id,
        )
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    state_store = WorkflowStateStore(Path(args.state_file))
    yabai = SubprocessYabaiClient(args.yabai_bin)
    service = WindowFocusedService(
        yabai=yabai,
        state_store=state_store,
        window_id=window_id,
    )

    try:
        service.run()
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


def background_window_exit_cleanup_main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser(
        prog="background_window_exit_cleanup",
        description=(
            "Handle one supported yabai lifecycle signal for tracked background "
            "workflow windows, removing the window from a matched background pool "
            "when it has been destroyed or leaves workflow eligibility."
        ),
    )
    parser.add_argument(
        "--window-id",
        default=os.environ.get("YABAI_WINDOW_ID"),
        help="Signaled yabai window id. Defaults to the YABAI_WINDOW_ID environment variable.",
    )
    parser.add_argument(
        "--event",
        help=(
            "Required yabai signal event name. Supported values: "
            + ", ".join(sorted(SUPPORTED_BACKGROUND_WINDOW_EXIT_EVENTS))
            + "."
        ),
    )
    args = parser.parse_args(argv)

    try:
        window_id = _parse_signal_window_id(
            command_name="background_window_exit_cleanup",
            raw_value=args.window_id,
        )
        event = _parse_signal_event(
            command_name="background_window_exit_cleanup",
            raw_value=args.event,
            supported_values=SUPPORTED_BACKGROUND_WINDOW_EXIT_EVENTS,
        )
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    state_store = WorkflowStateStore(Path(args.state_file))
    yabai = SubprocessYabaiClient(args.yabai_bin)
    service = BackgroundWindowExitCleanupService(
        yabai=yabai,
        state_store=state_store,
        window_id=window_id,
        event=event,
    )

    try:
        service.run()
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


def background_window_return_as_new_main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser(
        prog="background_window_return_as_new",
        description=(
            "Handle one supported yabai lifecycle signal for a formerly removed "
            "background workflow window that has become eligible again, reusing "
            "the tracked new-window placement path when its current workflow "
            "space is already persisted."
        ),
    )
    parser.add_argument(
        "--window-id",
        default=os.environ.get("YABAI_WINDOW_ID"),
        help="Signaled yabai window id. Defaults to the YABAI_WINDOW_ID environment variable.",
    )
    parser.add_argument(
        "--event",
        help=(
            "Required yabai signal event name. Supported values: "
            + ", ".join(sorted(SUPPORTED_BACKGROUND_WINDOW_RETURN_EVENTS))
            + "."
        ),
    )
    args = parser.parse_args(argv)

    try:
        window_id = _parse_signal_window_id(
            command_name="background_window_return_as_new",
            raw_value=args.window_id,
        )
        event = _parse_signal_event(
            command_name="background_window_return_as_new",
            raw_value=args.event,
            supported_values=SUPPORTED_BACKGROUND_WINDOW_RETURN_EVENTS,
        )
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    state_store = WorkflowStateStore(Path(args.state_file))
    yabai = SubprocessYabaiClient(args.yabai_bin)
    service = BackgroundWindowReturnAsNewService(
        yabai=yabai,
        state_store=state_store,
        window_id=window_id,
        event=event,
    )

    try:
        service.run()
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


def _run_service_command(
    *,
    argv: Optional[List[str]],
    prog: str,
    description: str,
    service_factory: Callable[..., object],
) -> int:
    parser = _build_parser(prog=prog, description=description)
    args = parser.parse_args(argv)

    state_store = WorkflowStateStore(Path(args.state_file))
    yabai = SubprocessYabaiClient(args.yabai_bin)
    service = service_factory(yabai=yabai, state_store=state_store)

    try:
        service.run()
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


def _build_parser(*, prog: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=description,
    )
    parser.add_argument(
        "--yabai-bin",
        default=os.environ.get("YABAI_BIN", "yabai"),
        help="Path to the yabai executable.",
    )
    parser.add_argument(
        "--state-file",
        default=os.environ.get("YHWM_STATE_FILE", str(WorkflowStateStore.default_path())),
        help="Path to the workflow state JSON file.",
    )
    return parser


def _parse_signal_window_id(*, command_name: str, raw_value: Optional[str]) -> int:
    if raw_value is None:
        raise WorkflowError(
            f"{command_name} requires a usable window id via --window-id or YABAI_WINDOW_ID."
        )

    candidate = raw_value.strip()
    if not candidate:
        raise WorkflowError(
            f"{command_name} requires a usable window id via --window-id or YABAI_WINDOW_ID."
        )

    try:
        window_id = int(candidate)
    except ValueError as exc:
        raise WorkflowError(
            f"{command_name} received an invalid window id: {raw_value!r}"
        ) from exc

    if window_id <= 0:
        raise WorkflowError(
            f"{command_name} received an invalid window id: {raw_value!r}"
        )

    return window_id


def _parse_signal_event(
    *,
    command_name: str,
    raw_value: Optional[str],
    supported_values: set[str] | frozenset[str],
) -> str:
    if raw_value is None:
        raise WorkflowError(f"{command_name} requires a supported event via --event.")

    candidate = raw_value.strip().lower()
    if not candidate:
        raise WorkflowError(f"{command_name} requires a supported event via --event.")

    if candidate not in supported_values:
        raise WorkflowError(
            f"{command_name} received an unsupported event: {raw_value!r}"
        )

    return candidate
