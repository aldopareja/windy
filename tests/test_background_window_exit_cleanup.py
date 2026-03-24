from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest

from runtime.yhwm.background_window_exit_cleanup import (
    BackgroundWindowExitCleanupService,
    WINDOW_DESTROYED_EVENT,
)
from runtime.yhwm.cli import background_window_exit_cleanup_main
from runtime.yhwm.errors import WorkflowError
from runtime.yhwm.state import WorkflowStateStore


class BackgroundWindowExitCleanupServiceTests(unittest.TestCase):
    def test_destroyed_background_window_is_removed_without_querying_destroyed_window(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102, 103],
                pending_split_direction="east",
            )
            client = FakeBackgroundWindowExitCleanupYabaiClient(
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    103: eligible_window(103),
                },
                space_displays={2: 1},
            )

            result = BackgroundWindowExitCleanupService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                window_id=102,
                event=WINDOW_DESTROYED_EVENT,
            ).run()

            self.assertEqual(result.action, "removed_destroyed_background_window")
            self.assertEqual(result.visible_window_id, 101)
            self.assertEqual(result.background_window_ids, [103])
            self.assertEqual(result.pending_split_direction, "east")
            self.assertEqual(client.queried_window_ids, [])

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["spaces"]["1:2"]["visible_window_id"], 101)
            self.assertEqual(payload["spaces"]["1:2"]["background_window_ids"], [103])
            self.assertEqual(payload["spaces"]["1:2"]["pending_split_direction"], "east")

    def test_non_destroyed_signal_removes_background_window_that_moved_out_of_space(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102],
                pending_split_direction="west",
            )
            client = FakeBackgroundWindowExitCleanupYabaiClient(
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102, display=2, space=5),
                },
                display_spaces={
                    1: [2],
                    2: [5],
                },
                space_displays={
                    2: 1,
                    5: 2,
                },
            )

            result = BackgroundWindowExitCleanupService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                window_id=102,
                event="window_moved",
            ).run()

            self.assertEqual(result.action, "removed_ineligible_background_window")
            self.assertEqual(result.visible_window_id, 101)
            self.assertEqual(result.background_window_ids, [])
            self.assertEqual(result.pending_split_direction, "west")
            self.assertEqual(client.queried_window_ids, [102])

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["spaces"]["1:2"]["visible_window_id"], 101)
            self.assertEqual(payload["spaces"]["1:2"]["background_window_ids"], [])
            self.assertEqual(payload["spaces"]["1:2"]["pending_split_direction"], "west")

    def test_non_destroyed_signal_keeps_background_window_when_still_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102, 103],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeBackgroundWindowExitCleanupYabaiClient(
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102),
                    103: eligible_window(103),
                },
                space_displays={2: 1},
            )

            result = BackgroundWindowExitCleanupService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                window_id=102,
                event="window_minimized",
            ).run()

            self.assertEqual(result.action, "ignored_still_eligible")
            self.assertEqual(result.visible_window_id, 101)
            self.assertEqual(result.background_window_ids, [102, 103])
            self.assertEqual(client.queried_window_ids, [102])
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)

    def test_untracked_window_id_leaves_state_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[103],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeBackgroundWindowExitCleanupYabaiClient(
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102),
                    103: eligible_window(103),
                },
                space_displays={2: 1},
            )

            result = BackgroundWindowExitCleanupService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                window_id=102,
                event=WINDOW_DESTROYED_EVENT,
            ).run()

            self.assertEqual(result.action, "ignored_untracked_background_window")
            self.assertEqual(client.queried_window_ids, [])
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)

    def test_tracked_visible_window_id_is_ignored_when_not_in_background_pool(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeBackgroundWindowExitCleanupYabaiClient(
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102),
                },
                space_displays={2: 1},
            )

            result = BackgroundWindowExitCleanupService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                window_id=101,
                event=WINDOW_DESTROYED_EVENT,
            ).run()

            self.assertEqual(result.action, "ignored_untracked_background_window")
            self.assertEqual(client.queried_window_ids, [])
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)

    def test_invalid_persisted_state_blocks_cleanup_without_partial_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            state_path.write_text(
                '{"schema_version":1,"spaces":{"1:2":{"display":1,"space":2,"visible_window_id":101,"background_window_ids":[102],"pending_split_direction":[]}}}\n',
                encoding="utf-8",
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeBackgroundWindowExitCleanupYabaiClient(
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102),
                },
                space_displays={2: 1},
            )

            with self.assertRaisesRegex(WorkflowError, "invalid schema"):
                BackgroundWindowExitCleanupService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                    window_id=102,
                    event=WINDOW_DESTROYED_EVENT,
                ).run()

            self.assertEqual(client.queried_window_ids, [])
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)

    def test_non_destroyed_cleanup_query_failure_blocks_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102, 103],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeBackgroundWindowExitCleanupYabaiClient(
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102),
                    103: eligible_window(103),
                },
                space_displays={2: 1},
                fail_on_query_window_id=102,
            )

            with self.assertRaisesRegex(WorkflowError, "Failed to query window 102"):
                BackgroundWindowExitCleanupService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                    window_id=102,
                    event="window_minimized",
                ).run()

            self.assertEqual(client.queried_window_ids, [102])
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)

    def test_duplicate_background_matches_block_partial_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entries(
                state_path,
                [
                    {
                        "display": 1,
                        "space": 2,
                        "visible_window_id": 101,
                        "background_window_ids": [102],
                    },
                    {
                        "display": 2,
                        "space": 5,
                        "visible_window_id": 201,
                        "background_window_ids": [102, 203],
                    },
                ],
            )
            original_payload = json.loads(state_path.read_text(encoding="utf-8"))
            client = FakeBackgroundWindowExitCleanupYabaiClient(
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    201: eligible_window(201, display=2, space=5),
                    203: eligible_window(203, display=2, space=5),
                },
                display_spaces={
                    1: [2],
                    2: [5],
                },
                space_displays={
                    2: 1,
                    5: 2,
                },
            )

            with self.assertRaisesRegex(
                WorkflowError,
                "more than one tracked background window pool",
            ):
                BackgroundWindowExitCleanupService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                    window_id=102,
                    event=WINDOW_DESTROYED_EVENT,
                ).run()

            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8")),
                original_payload,
            )

    def test_destroyed_cleanup_requires_matched_space_to_remain_visible_bsp(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeBackgroundWindowExitCleanupYabaiClient(
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                },
                space_displays={2: 1},
                space_layouts={2: "stack"},
            )

            with self.assertRaisesRegex(WorkflowError, "must use layout 'bsp'"):
                BackgroundWindowExitCleanupService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                    window_id=102,
                    event=WINDOW_DESTROYED_EVENT,
                ).run()

            self.assertEqual(client.queried_window_ids, [])
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)


class BackgroundWindowExitCleanupCliTests(unittest.TestCase):
    def test_missing_event_returns_failure(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = background_window_exit_cleanup_main(
                [
                    "--window-id",
                    "102",
                    "--state-file",
                    "/tmp/unused-state.json",
                    "--yabai-bin",
                    "/usr/bin/false",
                ]
            )

        self.assertEqual(exit_code, 1)


class FakeBackgroundWindowExitCleanupYabaiClient:
    def __init__(
        self,
        *,
        window_records: dict[int, dict],
        display_spaces: dict[int, list[int]] | None = None,
        space_displays: dict[int, int] | None = None,
        focus_follows_mouse: str = "off",
        mouse_follows_focus: str = "off",
        space_layouts: dict[int, str] | None = None,
        space_visibilities: dict[int, bool] | None = None,
        fail_on_query_window_id: int | None = None,
    ) -> None:
        self.window_records = {
            window_id: dict(record) for window_id, record in window_records.items()
        }
        self.display_spaces = display_spaces or {1: [2]}
        self.space_displays = space_displays or {2: 1}
        self.focus_follows_mouse = focus_follows_mouse
        self.mouse_follows_focus = mouse_follows_focus
        self.space_layouts = space_layouts or {}
        self.space_visibilities = space_visibilities or {}
        self.fail_on_query_window_id = fail_on_query_window_id
        self.queried_window_ids: list[int] = []

    def get_config(self, setting: str, *, space: int | None = None) -> str:
        if setting == "focus_follows_mouse":
            return self.focus_follows_mouse
        if setting == "mouse_follows_focus":
            return self.mouse_follows_focus
        if setting == "layout":
            if space is None:
                raise AssertionError("layout queries must include a space")
            return self.space_layouts.get(space, "bsp")
        raise AssertionError(f"Unexpected config request: {setting}")

    def query_window(self, window_id: int) -> dict:
        self.queried_window_ids.append(window_id)
        if self.fail_on_query_window_id == window_id:
            raise WorkflowError(f"Failed to query window {window_id} from yabai")
        return dict(self.window_records[window_id])

    def query_display(self, display: int) -> dict:
        return {
            "index": display,
            "has-focus": True,
            "spaces": list(self.display_spaces.get(display, [])),
        }

    def query_space(self, space: int) -> dict:
        return {
            "index": space,
            "display": self.space_displays[space],
            "windows": [],
            "is-visible": self.space_visibilities.get(space, True),
            "is-native-fullscreen": False,
        }

    def query_focused_window(self) -> dict:
        raise AssertionError("query_focused_window should not be used in cleanup tests")

    def query_recent_window(self) -> dict:
        raise AssertionError("query_recent_window should not be used in cleanup tests")

    def query_windows_for_space(self, space: int) -> list[dict]:
        raise AssertionError("query_windows_for_space should not be used in cleanup tests")

    def set_space_layout(self, space: int, layout: str) -> None:
        raise AssertionError("set_space_layout should not be used in cleanup tests")

    def stack_window(self, anchor_window_id: int, candidate_window_id: int) -> None:
        raise AssertionError("stack_window should not be used in cleanup tests")

    def promote_stacked_window(self, window_id: int, direction: str) -> None:
        raise AssertionError("promote_stacked_window should not be used in cleanup tests")

    def arm_window_split(self, window_id: int, direction: str) -> None:
        raise AssertionError("arm_window_split should not be used in cleanup tests")

    def focus_window(self, window_id: int) -> None:
        raise AssertionError("focus_window should not be used in cleanup tests")


def write_state_entry(
    path: Path,
    *,
    visible_window_id: int,
    background_window_ids: list[int],
    pending_split_direction: str | None = None,
    display: int = 1,
    space: int = 2,
) -> None:
    entry = {
        "display": display,
        "space": space,
        "visible_window_id": visible_window_id,
        "background_window_ids": background_window_ids,
    }
    if pending_split_direction is not None:
        entry["pending_split_direction"] = pending_split_direction

    write_state_entries(path, [entry])


def write_state_entries(path: Path, entries: list[dict[str, object]]) -> None:
    spaces: dict[str, dict[str, object]] = {}
    for entry in entries:
        display = int(entry["display"])
        space = int(entry["space"])
        spaces[f"{display}:{space}"] = dict(entry)

    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "spaces": spaces,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def eligible_window(window_id: int, **overrides: object) -> dict:
    base = {
        "id": window_id,
        "display": 1,
        "space": 2,
        "root-window": True,
        "role": "AXWindow",
        "subrole": "AXStandardWindow",
        "can-move": True,
        "has-ax-reference": True,
        "level": 0,
        "layer": "normal",
        "has-focus": False,
        "is-floating": False,
        "is-sticky": False,
        "is-native-fullscreen": False,
        "is-minimized": False,
        "is-hidden": False,
    }
    base.update(overrides)
    return base


if __name__ == "__main__":
    unittest.main()
