from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest

from runtime.yhwm.background_window_return_as_new import (
    BackgroundWindowReturnAsNewService,
)
from runtime.yhwm.cli import background_window_return_as_new_main
from runtime.yhwm.errors import WorkflowError
from runtime.yhwm.state import WorkflowStateStore


class BackgroundWindowReturnAsNewServiceTests(unittest.TestCase):
    def test_returning_window_stacks_onto_focused_tile_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[103],
            )
            client = FakeBackgroundWindowReturnAsNewYabaiClient(
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102),
                    103: eligible_window(103),
                },
                space_windows={2: [101, 102, 103]},
            )

            result = BackgroundWindowReturnAsNewService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                window_id=102,
                event="window_deminimized",
            ).run()

            self.assertEqual(result.action, "stacked_on_focused_tile")
            self.assertEqual(result.visible_window_id, 102)
            self.assertEqual(result.background_window_ids, [103])
            self.assertEqual(result.pending_split_direction, None)
            self.assertEqual(client.actions, [("stack", 101, 102)])

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["spaces"]["1:2"]["visible_window_id"], 102)
            self.assertEqual(payload["spaces"]["1:2"]["background_window_ids"], [103])
            self.assertNotIn("pending_split_direction", payload["spaces"]["1:2"])

    def test_returning_window_consumes_pending_split_and_clears_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[103],
                pending_split_direction="east",
            )
            client = FakeBackgroundWindowReturnAsNewYabaiClient(
                window_records={
                    101: eligible_window(101),
                    102: eligible_window(102, **{"has-focus": True}),
                    103: eligible_window(103),
                },
                space_windows={2: [101, 102, 103]},
                fail_on_recent_query=True,
            )

            result = BackgroundWindowReturnAsNewService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                window_id=102,
                event="window_deminimized",
            ).run()

            self.assertEqual(result.action, "consumed_pending_split")
            self.assertEqual(result.visible_window_id, 102)
            self.assertEqual(result.background_window_ids, [103])
            self.assertEqual(result.pending_split_direction, None)
            self.assertEqual(client.actions, [])

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["spaces"]["1:2"]["visible_window_id"], 102)
            self.assertEqual(payload["spaces"]["1:2"]["background_window_ids"], [103])
            self.assertNotIn("pending_split_direction", payload["spaces"]["1:2"])

    def test_tracked_visible_window_is_ignored_without_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[103],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeBackgroundWindowReturnAsNewYabaiClient(
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    103: eligible_window(103),
                },
                space_windows={2: [101, 103]},
            )

            result = BackgroundWindowReturnAsNewService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                window_id=101,
                event="window_deminimized",
            ).run()

            self.assertEqual(result.action, "ignored_already_tracked")
            self.assertEqual(result.visible_window_id, 101)
            self.assertEqual(result.background_window_ids, [103])
            self.assertEqual(client.actions, [])
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)

    def test_tracked_background_window_in_another_space_is_ignored_without_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entries(
                state_path,
                [
                    {
                        "display": 1,
                        "space": 2,
                        "visible_window_id": 101,
                        "background_window_ids": [103],
                    },
                    {
                        "display": 2,
                        "space": 5,
                        "visible_window_id": 201,
                        "background_window_ids": [102, 202],
                    },
                ],
            )
            original_payload = json.loads(state_path.read_text(encoding="utf-8"))
            client = FakeBackgroundWindowReturnAsNewYabaiClient(
                window_records={
                    101: eligible_window(101),
                    102: eligible_window(102, **{"has-focus": True}),
                    103: eligible_window(103),
                    201: eligible_window(201, display=2, space=5),
                    202: eligible_window(202, display=2, space=5),
                },
                space_windows={
                    2: [101, 102, 103],
                    5: [201, 202],
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

            result = BackgroundWindowReturnAsNewService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                window_id=102,
                event="window_deminimized",
            ).run()

            self.assertEqual(result.action, "ignored_already_tracked")
            self.assertEqual(result.visible_window_id, 101)
            self.assertEqual(result.background_window_ids, [103])
            self.assertEqual(client.actions, [])
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8")),
                original_payload,
            )

    def test_untracked_space_is_ignored_without_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[103],
            )
            original_payload = json.loads(state_path.read_text(encoding="utf-8"))
            client = FakeBackgroundWindowReturnAsNewYabaiClient(
                window_records={
                    101: eligible_window(101),
                    103: eligible_window(103),
                    201: eligible_window(201, display=2, space=5, **{"has-focus": True}),
                },
                space_windows={
                    2: [101, 103],
                    5: [201],
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

            result = BackgroundWindowReturnAsNewService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                window_id=201,
                event="window_deminimized",
            ).run()

            self.assertEqual(result.action, "ignored_untracked_space")
            self.assertEqual(client.actions, [])
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8")),
                original_payload,
            )

    def test_ineligible_returning_window_is_ignored_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[103],
                pending_split_direction="east",
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeBackgroundWindowReturnAsNewYabaiClient(
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102, **{"is-hidden": True}),
                    103: eligible_window(103),
                },
                space_windows={2: [101, 102, 103]},
            )

            result = BackgroundWindowReturnAsNewService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                window_id=102,
                event="window_deminimized",
            ).run()

            self.assertEqual(result.action, "ignored_ineligible")
            self.assertEqual(client.actions, [])
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)

    def test_invalid_state_blocks_mutation_before_return_placement(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            state_path.write_text(
                '{"schema_version":1,"spaces":{"1:2":{"display":1,"space":2,"visible_window_id":101,"background_window_ids":[103],"pending_split_direction":[]}}}\n',
                encoding="utf-8",
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeBackgroundWindowReturnAsNewYabaiClient(
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102),
                    103: eligible_window(103),
                },
                space_windows={2: [101, 102, 103]},
            )

            with self.assertRaisesRegex(WorkflowError, "invalid schema"):
                BackgroundWindowReturnAsNewService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                    window_id=102,
                    event="window_deminimized",
                ).run()

            self.assertEqual(client.actions, [])
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)

    def test_query_failure_blocks_mutation_without_partial_update(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[103],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeBackgroundWindowReturnAsNewYabaiClient(
                window_records={
                    101: eligible_window(101),
                    102: eligible_window(102),
                    103: eligible_window(103),
                },
                space_windows={2: [101, 102, 103]},
                fail_on_query_window_id=102,
            )

            with self.assertRaisesRegex(WorkflowError, "Failed to query window 102"):
                BackgroundWindowReturnAsNewService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                    window_id=102,
                    event="window_deminimized",
                ).run()

            self.assertEqual(client.actions, [])
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)

    def test_unsupported_event_is_rejected_before_window_queries(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[103],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeBackgroundWindowReturnAsNewYabaiClient(
                window_records={
                    102: eligible_window(102),
                },
                space_windows={2: [101, 102, 103]},
                fail_on_query_window_id=102,
            )

            with self.assertRaisesRegex(WorkflowError, "unsupported event"):
                BackgroundWindowReturnAsNewService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                    window_id=102,
                    event="window_moved",
                ).run()

            self.assertEqual(client.actions, [])
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)


class BackgroundWindowReturnAsNewCliTests(unittest.TestCase):
    def test_missing_event_returns_failure(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = background_window_return_as_new_main(
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

    def test_unsupported_event_returns_failure(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = background_window_return_as_new_main(
                [
                    "--window-id",
                    "102",
                    "--event",
                    "window_moved",
                    "--state-file",
                    "/tmp/unused-state.json",
                    "--yabai-bin",
                    "/usr/bin/false",
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("unsupported event", stderr.getvalue())


class FakeBackgroundWindowReturnAsNewYabaiClient:
    def __init__(
        self,
        *,
        window_records: dict[int, dict],
        space_windows: dict[int, list[int]],
        recent_window_id: int | None = None,
        display_spaces: dict[int, list[int]] | None = None,
        space_displays: dict[int, int] | None = None,
        focus_follows_mouse: str = "off",
        mouse_follows_focus: str = "off",
        space_layouts: dict[int, str] | None = None,
        space_visibilities: dict[int, bool] | None = None,
        fail_on_query_window_id: int | None = None,
        fail_on_recent_query: bool = False,
    ) -> None:
        self.recent_window_id = recent_window_id
        self.window_records = {
            window_id: dict(record) for window_id, record in window_records.items()
        }
        self.space_windows = {
            space: list(window_ids) for space, window_ids in space_windows.items()
        }
        self.display_spaces = display_spaces or {1: sorted(self.space_windows)}
        self.space_displays = space_displays or {
            space: 1 for space in self.space_windows
        }
        self.focus_follows_mouse = focus_follows_mouse
        self.mouse_follows_focus = mouse_follows_focus
        self.space_layouts = space_layouts or {}
        self.space_visibilities = space_visibilities or {}
        self.fail_on_query_window_id = fail_on_query_window_id
        self.fail_on_recent_query = fail_on_recent_query
        self.actions: list[tuple] = []

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

    def query_focused_window(self) -> dict:
        raise AssertionError("query_focused_window should not be used in return tests")

    def query_window(self, window_id: int) -> dict:
        if self.fail_on_query_window_id == window_id:
            raise WorkflowError(f"Failed to query window {window_id} from yabai")
        return dict(self.window_records[window_id])

    def query_recent_window(self) -> dict:
        if self.fail_on_recent_query or self.recent_window_id is None:
            raise WorkflowError("Failed to query the most recently focused window from yabai")
        return dict(self.window_records[self.recent_window_id])

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
            "windows": list(self.space_windows.get(space, [])),
            "is-visible": self.space_visibilities.get(space, True),
            "is-native-fullscreen": False,
        }

    def query_windows_for_space(self, space: int) -> list[dict]:
        return [
            dict(self.window_records[window_id])
            for window_id in self.space_windows.get(space, [])
        ]

    def set_space_layout(self, space: int, layout: str) -> None:
        self.actions.append(("set_layout", space, layout))

    def stack_window(self, anchor_window_id: int, candidate_window_id: int) -> None:
        self.actions.append(("stack", anchor_window_id, candidate_window_id))

    def promote_stacked_window(self, window_id: int, direction: str) -> None:
        self.actions.append(("promote_stacked", window_id, direction))

    def arm_window_split(self, window_id: int, direction: str) -> None:
        self.actions.append(("insert", window_id, direction))

    def focus_window(self, window_id: int) -> None:
        self.actions.append(("focus", window_id))


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
        "stack-index": 0,
        "has-focus": False,
        "is-visible": True,
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
