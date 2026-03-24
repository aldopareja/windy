from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import List, Optional

from .collapse import CollapseCurrentSpaceService
from .errors import WorkflowError
from .state import WorkflowStateStore
from .yabai import SubprocessYabaiClient


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    state_store = WorkflowStateStore(Path(args.state_file))
    yabai = SubprocessYabaiClient(args.yabai_bin)
    service = CollapseCurrentSpaceService(yabai=yabai, state_store=state_store)

    try:
        service.run()
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collapse_current_space",
        description=(
            "Collapse the current eligible workflow space so the focused eligible "
            "window stays visible and other eligible windows become background "
            "stack members."
        ),
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
