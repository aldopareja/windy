from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Callable, List, Optional

from .collapse import CollapseCurrentSpaceService
from .errors import WorkflowError
from .state import WorkflowStateStore
from .split import SplitFromBackgroundPoolService
from .window_created import WindowCreatedService
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
        window_id = _parse_window_id(args.window_id)
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


def _parse_window_id(raw_value: Optional[str]) -> int:
    if raw_value is None:
        raise WorkflowError(
            "window_created requires a usable window id via --window-id or YABAI_WINDOW_ID."
        )

    candidate = raw_value.strip()
    if not candidate:
        raise WorkflowError(
            "window_created requires a usable window id via --window-id or YABAI_WINDOW_ID."
        )

    try:
        window_id = int(candidate)
    except ValueError as exc:
        raise WorkflowError(
            f"window_created received an invalid window id: {raw_value!r}"
        ) from exc

    if window_id <= 0:
        raise WorkflowError(
            f"window_created received an invalid window id: {raw_value!r}"
        )

    return window_id
