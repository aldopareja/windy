from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys
from typing import Optional, Sequence

from .errors import WorkflowError
from .hammerspoon import SubprocessHammerspoonClient
from .integration import install_hammerspoon, remove_legacy_yabai_signals
from .models import NormalizedFrame
from .state import RuntimeStateStore
from .workflow import SUPPORTED_NAVIGATION_DIRECTIONS, SUPPORTED_SPLIT_DIRECTIONS, WorkflowRuntime
from .yabai import SubprocessYabaiClient


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="yhwm",
        description="Live-derived workflow runtime for the current yabai/Hammerspoon workflow.",
    )
    parser.add_argument(
        "--state-file",
        default=str(RuntimeStateStore.default_path()),
        help="State file path. Defaults to runtime/state/yhwm-state-v4.json.",
    )
    parser.add_argument(
        "--yabai-bin",
        default=_default_executable_path("yabai"),
        help="Path to the yabai executable.",
    )
    parser.add_argument(
        "--hs-bin",
        default=_default_executable_path("hs"),
        help="Path to the Hammerspoon CLI executable.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "reseed",
        help="Collapse the current space to one visible leader plus background stacks.",
    )
    subparsers.add_parser(
        "float",
        help="Convert the current tracked space to float layout and stop tracking it.",
    )
    subparsers.add_parser(
        "delete-tile",
        help="Merge the focused live tile back into another visible tile.",
    )

    navigate_parser = subparsers.add_parser(
        "navigate",
        help="Focus a visible neighbor in the requested direction.",
    )
    navigate_parser.add_argument(
        "--direction",
        required=True,
        choices=sorted(SUPPORTED_NAVIGATION_DIRECTIONS),
        help="Navigation direction: north, east, south, or west.",
    )

    split_parser = subparsers.add_parser(
        "split",
        help="Split the focused live tile or arm the next split.",
    )
    split_parser.add_argument(
        "--direction",
        required=True,
        choices=sorted(SUPPORTED_SPLIT_DIRECTIONS),
        help="Split direction: east for left/right, south for top/bottom.",
    )

    alttab_parser = subparsers.add_parser(
        "alttab",
        help="Commit an Alt-Tab selection against the live topology.",
    )
    alttab_parser.add_argument("--origin-window-id", required=True, help="Focused window id when Alt-Tab opened.")
    alttab_parser.add_argument("--selected-window-id", required=True, help="Focused window id after Alt-Tab closed.")
    alttab_parser.add_argument(
        "--origin-open-frame",
        required=True,
        help="Origin tile frame at chooser open as x,y,w,h.",
    )
    alttab_parser.add_argument(
        "--selected-open-frame",
        required=True,
        help="Selected window frame at chooser open as x,y,w,h.",
    )
    alttab_parser.add_argument(
        "--selected-was-visible-at-open",
        action="store_true",
        help="Whether the selected window was the frontmost member of its frame group when Alt-Tab opened.",
    )

    install_parser = subparsers.add_parser(
        "install",
        help="Install the repo-managed Hammerspoon loader and reload Hammerspoon.",
    )
    install_subparsers = install_parser.add_subparsers(dest="install_command", required=True)
    install_subparsers.add_parser(
        "hammerspoon",
        help="Install the repo-managed Hammerspoon loader and reload Hammerspoon.",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)

    state_store = RuntimeStateStore(Path(args.state_file))
    yabai = SubprocessYabaiClient(args.yabai_bin)
    hammerspoon = SubprocessHammerspoonClient(args.hs_bin)
    runtime = WorkflowRuntime(yabai=yabai, hammerspoon=hammerspoon, state_store=state_store)

    try:
        if args.command == "reseed":
            runtime.reseed()
        elif args.command == "float":
            runtime.float_space()
        elif args.command == "delete-tile":
            runtime.delete_tile()
        elif args.command == "navigate":
            runtime.navigate(args.direction)
        elif args.command == "split":
            runtime.split(args.direction)
        elif args.command == "alttab":
            runtime.alttab(
                origin_window_id=_parse_window_id("alttab", args.origin_window_id),
                selected_window_id=_parse_window_id("alttab", args.selected_window_id),
                origin_open_frame=_parse_frame(args.origin_open_frame),
                selected_open_frame=_parse_frame(args.selected_open_frame),
                selected_was_visible_at_open=bool(args.selected_was_visible_at_open),
            )
        elif args.command == "install":
            executable_path = str(Path(__file__).resolve().parents[1] / "bin" / "yhwm")
            if args.install_command == "hammerspoon":
                remove_legacy_yabai_signals(yabai=yabai)
                install_hammerspoon(
                    runtime_root=Path(__file__).resolve().parents[1],
                    executable_path=executable_path,
                    hs_bin=args.hs_bin,
                )
            else:
                raise WorkflowError(f"Unsupported install command: {args.install_command}")
        else:
            raise WorkflowError(f"Unsupported command: {args.command}")
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


def _parse_window_id(command_name: str, raw_value: object) -> int:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise WorkflowError(f"{command_name} requires a positive integer window id.")
    try:
        window_id = int(raw_value)
    except ValueError as exc:
        raise WorkflowError(f"{command_name} requires a positive integer window id.") from exc
    if window_id <= 0:
        raise WorkflowError(f"{command_name} requires a positive integer window id.")
    return window_id


def _parse_frame(raw_value: object) -> NormalizedFrame:
    if not isinstance(raw_value, str):
        raise WorkflowError("Expected a frame string formatted as x,y,w,h.")
    parts = [part.strip() for part in raw_value.split(",")]
    if len(parts) != 4:
        raise WorkflowError("Expected a frame string formatted as x,y,w,h.")
    try:
        x, y, w, h = (int(part) for part in parts)
    except ValueError as exc:
        raise WorkflowError("Expected a frame string formatted as x,y,w,h.") from exc
    if w <= 0 or h <= 0:
        raise WorkflowError("Expected a frame string formatted as x,y,w,h.")
    return NormalizedFrame(x=x, y=y, w=w, h=h)


def _default_executable_path(name: str) -> str:
    for candidate in (
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
    ):
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate

    discovered = shutil.which(name)
    if discovered:
        return discovered
    return name


if __name__ == "__main__":
    raise SystemExit(main())
