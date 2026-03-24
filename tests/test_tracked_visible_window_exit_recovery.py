from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest

from runtime.yhwm.cli import tracked_visible_window_exit_recovery_main
from runtime.yhwm.errors import WorkflowError
from runtime.yhwm.split import DEFAULT_PENDING_SPLIT_DIRECTION
from runtime.yhwm.state import WorkflowStateStore
from runtime.yhwm.tracked_visible_window_exit_recovery import (
    TrackedVisibleWindowExitRecoveryService,
    WINDOW_DESTROYED_EVENT,
)


class TrackedVisibleWindowExitRecoveryServiceTests(unittest.TestCase):
    def test_background_candidate_is_promoted_and_removed_from_background_pool(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102, 103],
            )
            client = FakeTrackedVisibleWindowExitRecoveryYabaiClient(
                window_records={
                    102: eligible_window(102, **{"stack-index": 1}),
                    103: eligible_window(103, **{"stack-index": 2}),
                    201: eligible_window(201, **{"stack-index": 0}),
                },
                space_windows={
                    2: [102, 103, 201],
                },
            )

            result = TrackedVisibleWindowExitRecoveryService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                window_id=101,
                event="window_minimized",
            ).run()

            self.assertEqual(result.action, "recovered_with_background_window")
            self.assertEqual(result.visible_window_id, 102)
            self.assertEqual(result.background_window_ids, [103])
            self.assertEqual(
                client.actions,
                [("promote_stacked", 102, DEFAULT_PENDING_SPLIT_DIRECTION)],
            )

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["spaces"]["1:2"]["visible_window_id"], 102)
            self.assertEqual(payload["spaces"]["1:2"]["background_window_ids"], [103])
            self.assertNotIn("pending_split_direction", payload["spaces"]["1:2"])

    def test_visible_background_candidate_is_retargeted_without_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102, 103],
            )
            client = FakeTrackedVisibleWindowExitRecoveryYabaiClient(
                window_records={
                    102: eligible_window(102, **{"stack-index": 0}),
                    103: eligible_window(103, **{"stack-index": 1}),
                    201: eligible_window(201, **{"stack-index": 0}),
                },
                space_windows={
                    2: [102, 103, 201],
                },
            )

            result = TrackedVisibleWindowExitRecoveryService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                window_id=101,
                event=WINDOW_DESTROYED_EVENT,
            ).run()

            self.assertEqual(result.action, "recovered_with_background_window")
            self.assertEqual(result.visible_window_id, 102)
            self.assertEqual(result.background_window_ids, [103])
            self.assertEqual(client.actions, [])

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["spaces"]["1:2"]["visible_window_id"], 102)
            self.assertEqual(payload["spaces"]["1:2"]["background_window_ids"], [103])

    def test_no_background_candidates_retargets_to_remaining_visible_window(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[],
            )
            client = FakeTrackedVisibleWindowExitRecoveryYabaiClient(
                window_records={
                    201: eligible_window(201, display=1, space=2),
                },
                space_windows={
                    2: [201],
                },
            )

            result = TrackedVisibleWindowExitRecoveryService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                window_id=101,
                event="window_moved",
            ).run()

            self.assertEqual(result.action, "retargeted_remaining_visible_window")
            self.assertEqual(result.visible_window_id, 201)
            self.assertEqual(result.background_window_ids, [])
            self.assertEqual(client.actions, [])

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["spaces"]["1:2"]["visible_window_id"], 201)
            self.assertEqual(payload["spaces"]["1:2"]["background_window_ids"], [])

    def test_no_remaining_eligible_windows_removes_only_matched_space_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entries(
                state_path,
                [
                    {
                        "display": 1,
                        "space": 2,
                        "visible_window_id": 101,
                        "background_window_ids": [],
                    },
                    {
                        "display": 2,
                        "space": 5,
                        "visible_window_id": 201,
                        "background_window_ids": [202],
                    },
                ],
            )
            client = FakeTrackedVisibleWindowExitRecoveryYabaiClient(
                window_records={
                    201: eligible_window(201, display=2, space=5),
                    202: eligible_window(202, display=2, space=5, **{"stack-index": 1}),
                },
                space_windows={
                    2: [],
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

            result = TrackedVisibleWindowExitRecoveryService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                window_id=101,
                event=WINDOW_DESTROYED_EVENT,
            ).run()

            self.assertEqual(result.action, "removed_empty_space_state")
            self.assertIsNone(result.visible_window_id)
            self.assertEqual(client.actions, [])

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("1:2", payload["spaces"])
            self.assertEqual(payload["spaces"]["2:5"]["visible_window_id"], 201)
            self.assertEqual(payload["spaces"]["2:5"]["background_window_ids"], [202])

    def test_untracked_window_id_leaves_state_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeTrackedVisibleWindowExitRecoveryYabaiClient(
                window_records={
                    102: eligible_window(102, **{"stack-index": 1}),
                },
                space_windows={
                    2: [102],
                },
            )

            result = TrackedVisibleWindowExitRecoveryService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                window_id=999,
                event=WINDOW_DESTROYED_EVENT,
            ).run()

            self.assertEqual(result.action, "ignored_untracked_visible_window")
            self.assertEqual(client.actions, [])
            self.assertEqual(client.queried_space_windows, [])
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)

    def test_window_moved_is_ignored_when_window_stays_eligible_in_same_space(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeTrackedVisibleWindowExitRecoveryYabaiClient(
                window_records={
                    101: eligible_window(101, **{"stack-index": 0}),
                    102: eligible_window(102, **{"stack-index": 1}),
                },
                space_windows={
                    2: [101, 102],
                },
            )

            result = TrackedVisibleWindowExitRecoveryService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                window_id=101,
                event="window_moved",
            ).run()

            self.assertEqual(result.action, "ignored_still_eligible")
            self.assertEqual(result.visible_window_id, 101)
            self.assertEqual(result.background_window_ids, [102])
            self.assertEqual(client.actions, [])
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)

    def test_pending_split_state_is_ignored_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102],
                pending_split_direction="east",
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeTrackedVisibleWindowExitRecoveryYabaiClient(
                window_records={
                    102: eligible_window(102, **{"stack-index": 1}),
                },
                space_windows={
                    2: [102],
                },
            )

            result = TrackedVisibleWindowExitRecoveryService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                window_id=101,
                event=WINDOW_DESTROYED_EVENT,
            ).run()

            self.assertEqual(result.action, "ignored_pending_split")
            self.assertEqual(result.pending_split_direction, "east")
            self.assertEqual(client.actions, [])
            self.assertEqual(client.queried_space_windows, [])
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)

    def test_invalid_persisted_state_blocks_mutation_without_partial_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            state_path.write_text(
                '{"schema_version":1,"spaces":{"1:2":{"display":1,"space":2,"visible_window_id":101,"background_window_ids":[102],"pending_split_direction":[]}}}\n',
                encoding="utf-8",
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeTrackedVisibleWindowExitRecoveryYabaiClient(
                window_records={
                    102: eligible_window(102, **{"stack-index": 1}),
                },
                space_windows={
                    2: [102],
                },
            )

            with self.assertRaisesRegex(WorkflowError, "invalid schema"):
                TrackedVisibleWindowExitRecoveryService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                    window_id=101,
                    event=WINDOW_DESTROYED_EVENT,
                ).run()

            self.assertEqual(client.actions, [])
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)

    def test_duplicate_visible_matches_block_partial_recovery(self) -> None:
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
                        "visible_window_id": 101,
                        "background_window_ids": [202],
                    },
                ],
            )
            original_payload = json.loads(state_path.read_text(encoding="utf-8"))
            client = FakeTrackedVisibleWindowExitRecoveryYabaiClient(
                window_records={
                    102: eligible_window(102, **{"stack-index": 1}),
                    202: eligible_window(202, display=2, space=5, **{"stack-index": 1}),
                },
                space_windows={
                    2: [102],
                    5: [202],
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
                "more than one tracked workflow space",
            ):
                TrackedVisibleWindowExitRecoveryService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                    window_id=101,
                    event=WINDOW_DESTROYED_EVENT,
                ).run()

            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8")),
                original_payload,
            )

    def test_query_failure_blocks_mutation_without_partial_update(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeTrackedVisibleWindowExitRecoveryYabaiClient(
                window_records={
                    102: eligible_window(102, **{"stack-index": 1}),
                },
                space_windows={
                    2: [102],
                },
                fail_on_query_space_windows=2,
            )

            with self.assertRaisesRegex(WorkflowError, "Failed to query windows for space 2"):
                TrackedVisibleWindowExitRecoveryService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                    window_id=101,
                    event="window_minimized",
                ).run()

            self.assertEqual(client.actions, [])
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)

    def test_promotion_failure_blocks_mutation_without_partial_update(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102, 103],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeTrackedVisibleWindowExitRecoveryYabaiClient(
                window_records={
                    102: eligible_window(102, **{"stack-index": 1}),
                    103: eligible_window(103, **{"stack-index": 2}),
                    201: eligible_window(201, **{"stack-index": 0}),
                },
                space_windows={
                    2: [102, 103, 201],
                },
                fail_on_promote=True,
            )

            with self.assertRaisesRegex(WorkflowError, "Failed to promote stacked window"):
                TrackedVisibleWindowExitRecoveryService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                    window_id=101,
                    event="window_minimized",
                ).run()

            self.assertEqual(
                client.actions,
                [("promote_stacked", 102, DEFAULT_PENDING_SPLIT_DIRECTION)],
            )
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)

    def test_tracked_space_must_remain_visible_bsp(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeTrackedVisibleWindowExitRecoveryYabaiClient(
                window_records={
                    102: eligible_window(102, **{"stack-index": 1}),
                },
                space_windows={
                    2: [102],
                },
                space_layouts={2: "stack"},
            )

            with self.assertRaisesRegex(WorkflowError, "must use layout 'bsp'"):
                TrackedVisibleWindowExitRecoveryService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                    window_id=101,
                    event=WINDOW_DESTROYED_EVENT,
                ).run()

            self.assertEqual(client.actions, [])
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)

    def test_unsupported_event_is_rejected_before_queries(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeTrackedVisibleWindowExitRecoveryYabaiClient(
                window_records={
                    102: eligible_window(102, **{"stack-index": 1}),
                },
                space_windows={
                    2: [102],
                },
                fail_on_query_space_windows=2,
            )

            with self.assertRaisesRegex(WorkflowError, "unsupported event"):
                TrackedVisibleWindowExitRecoveryService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                    window_id=101,
                    event="window_deminimized",
                ).run()

            self.assertEqual(client.actions, [])
            self.assertEqual(client.queried_space_windows, [])
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)


class TrackedVisibleWindowExitRecoveryCliTests(unittest.TestCase):
    def test_missing_event_returns_failure(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = tracked_visible_window_exit_recovery_main(
                [
                    "--window-id",
                    "101",
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
            exit_code = tracked_visible_window_exit_recovery_main(
                [
                    "--window-id",
                    "101",
                    "--event",
                    "window_deminimized",
                    "--state-file",
                    "/tmp/unused-state.json",
                    "--yabai-bin",
                    "/usr/bin/false",
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("unsupported event", stderr.getvalue())


class FakeTrackedVisibleWindowExitRecoveryYabaiClient:
    def __init__(
        self,
        *,
        window_records: dict[int, dict],
        space_windows: dict[int, list[int]],
        display_spaces: dict[int, list[int]] | None = None,
        space_displays: dict[int, int] | None = None,
        focus_follows_mouse: str = "off",
        mouse_follows_focus: str = "off",
        space_layouts: dict[int, str] | None = None,
        space_visibilities: dict[int, bool] | None = None,
        fail_on_query_space_windows: int | None = None,
        fail_on_promote: bool = False,
    ) -> None:
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
        self.fail_on_query_space_windows = fail_on_query_space_windows
        self.fail_on_promote = fail_on_promote
        self.actions: list[tuple] = []
        self.queried_space_windows: list[int] = []

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
        raise AssertionError(
            "query_focused_window should not be used in tracked visible exit tests"
        )

    def query_window(self, window_id: int) -> dict:
        raise AssertionError(
            "query_window should not be used in tracked visible exit tests"
        )

    def query_recent_window(self) -> dict:
        raise AssertionError(
            "query_recent_window should not be used in tracked visible exit tests"
        )

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
        self.queried_space_windows.append(space)
        if self.fail_on_query_space_windows == space:
            raise WorkflowError(f"Failed to query windows for space {space} from yabai")
        return [
            dict(self.window_records[window_id])
            for window_id in self.space_windows.get(space, [])
        ]

    def set_space_layout(self, space: int, layout: str) -> None:
        raise AssertionError(
            "set_space_layout should not be used in tracked visible exit tests"
        )

    def stack_window(self, anchor_window_id: int, candidate_window_id: int) -> None:
        raise AssertionError(
            "stack_window should not be used in tracked visible exit tests"
        )

    def promote_stacked_window(self, window_id: int, direction: str) -> None:
        self.actions.append(("promote_stacked", window_id, direction))
        if self.fail_on_promote:
            raise WorkflowError(
                "Failed to promote stacked window "
                f"{window_id} into a sibling {direction} split"
            )

    def arm_window_split(self, window_id: int, direction: str) -> None:
        raise AssertionError(
            "arm_window_split should not be used in tracked visible exit tests"
        )

    def focus_window(self, window_id: int) -> None:
        raise AssertionError(
            "focus_window should not be used in tracked visible exit tests"
        )


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
