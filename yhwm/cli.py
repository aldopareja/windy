from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys
from typing import Optional, Sequence

from .errors import WorkflowError
from .integration import install_hammerspoon, install_yabai_signals
from .state import RuntimeStateStore
from .workflow import (
    SUPPORTED_ALTTAB_CANCEL_REASONS,
    SUPPORTED_SIGNAL_WINDOW_EVENTS,
    SUPPORTED_SPLIT_DIRECTIONS,
    WorkflowRuntime,
)
from .yabai import SubprocessYabaiClient


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="yhwm",
        description="Single-binary v2 runtime for the current yabai/Hammerspoon workflow.",
    )
    parser.add_argument(
        "--state-file",
        default=str(RuntimeStateStore.default_path()),
        help="State file path. Defaults to runtime/state/yhwm-state-v2.json.",
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
        help="Collapse the current space to one visible leader plus a background pool.",
    )

    split_parser = subparsers.add_parser(
        "split",
        help="Split the current leader tile or arm a pending split.",
    )
    split_parser.add_argument(
        "--direction",
        required=True,
        choices=sorted(SUPPORTED_SPLIT_DIRECTIONS),
        help="Split direction: east for left/right, south for top/bottom.",
    )

    signal_parser = subparsers.add_parser(
        "signal",
        help="Handle live yabai signals.",
    )
    signal_subparsers = signal_parser.add_subparsers(dest="signal_command", required=True)

    signal_focus = signal_subparsers.add_parser(
        "focus",
        help="Handle a yabai window_focused signal.",
    )
    signal_focus.add_argument(
        "--window-id",
        default=os.environ.get("YABAI_WINDOW_ID"),
        help="Focused window id. Defaults to YABAI_WINDOW_ID.",
    )

    signal_window = signal_subparsers.add_parser(
        "window",
        help="Handle a yabai window lifecycle signal.",
    )
    signal_window.add_argument(
        "--event",
        required=True,
        choices=sorted(SUPPORTED_SIGNAL_WINDOW_EVENTS),
        help="Window lifecycle event.",
    )
    signal_window.add_argument(
        "--window-id",
        default=os.environ.get("YABAI_WINDOW_ID"),
        help="Window id. Defaults to YABAI_WINDOW_ID.",
    )

    alttab_parser = subparsers.add_parser(
        "alttab",
        help="Handle the AltTab workflow boundary.",
    )
    alttab_subparsers = alttab_parser.add_subparsers(dest="alttab_command", required=True)
    alttab_subparsers.add_parser("open", help="Arm an AltTab session from the current leader.")

    alttab_release = alttab_subparsers.add_parser(
        "release",
        help="Commit an AltTab session using the finally focused window.",
    )
    alttab_release.add_argument(
        "--window-id",
        help="Focused window id after AltTab closes. Falls back to the current focus query.",
    )

    alttab_cancel = alttab_subparsers.add_parser(
        "cancel",
        help="Cancel an AltTab session.",
    )
    alttab_cancel.add_argument(
        "--reason",
        required=True,
        choices=sorted(SUPPORTED_ALTTAB_CANCEL_REASONS),
        help="Cancellation reason.",
    )
    alttab_cancel.add_argument(
        "--window-id",
        help="Optional focused window id to guard the next follow-on focus signal.",
    )

    install_parser = subparsers.add_parser(
        "install",
        help="Install live integration hooks.",
    )
    install_subparsers = install_parser.add_subparsers(dest="install_command", required=True)
    install_subparsers.add_parser(
        "yabai-signals",
        help="Install the repo-managed yabai signals for v2.",
    )
    install_subparsers.add_parser(
        "hammerspoon",
        help="Install the repo-managed Hammerspoon loader and reload Hammerspoon.",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)

    state_store = RuntimeStateStore(Path(args.state_file))
    yabai = SubprocessYabaiClient(args.yabai_bin)
    runtime = WorkflowRuntime(yabai=yabai, state_store=state_store)

    try:
        if args.command == "reseed":
            runtime.reseed()
        elif args.command == "split":
            runtime.split(args.direction)
        elif args.command == "signal":
            if args.signal_command == "focus":
                runtime.handle_focus(_parse_window_id("signal focus", args.window_id))
            else:
                runtime.handle_window_event(
                    event=args.event,
                    window_id=_parse_window_id("signal window", args.window_id),
                )
        elif args.command == "alttab":
            if args.alttab_command == "open":
                runtime.alttab_open()
            elif args.alttab_command == "release":
                runtime.alttab_release(_parse_optional_window_id(args.window_id))
            else:
                runtime.alttab_cancel(
                    reason=args.reason,
                    window_id=_parse_optional_window_id(args.window_id),
                )
        elif args.command == "install":
            executable_path = str(Path(__file__).resolve().parents[1] / "bin" / "yhwm")
            if args.install_command == "yabai-signals":
                install_yabai_signals(yabai=yabai, executable_path=executable_path)
            else:
                install_hammerspoon(
                    runtime_root=Path(__file__).resolve().parents[1],
                    executable_path=executable_path,
                    hs_bin=args.hs_bin,
                )
        else:
            raise WorkflowError(f"Unsupported command: {args.command}")
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


def _parse_window_id(command_name: str, raw_value: object) -> int:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise WorkflowError(
            f"{command_name} requires --window-id or YABAI_WINDOW_ID to be set."
        )
    try:
        window_id = int(raw_value)
    except ValueError as exc:
        raise WorkflowError(f"{command_name} requires an integer window id.") from exc
    if window_id <= 0:
        raise WorkflowError(f"{command_name} requires a positive integer window id.")
    return window_id


def _parse_optional_window_id(raw_value: object) -> Optional[int]:
    if raw_value is None:
        return None
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    try:
        window_id = int(raw_value)
    except ValueError as exc:
        raise WorkflowError("Expected --window-id to be an integer.") from exc
    if window_id <= 0:
        raise WorkflowError("Expected --window-id to be a positive integer.")
    return window_id


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
