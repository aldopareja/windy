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
