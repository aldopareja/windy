from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
from typing import Optional
import unittest
from unittest.mock import patch

from runtime.yhwm.collapse import CollapseCurrentSpaceService
from runtime.yhwm.errors import WorkflowError
from runtime.yhwm.state import WorkflowStateStore
from runtime.yhwm.split import (
    DEFAULT_PENDING_SPLIT_DIRECTION,
    SplitFromBackgroundPoolService,
)
from runtime.yhwm.yabai import SubprocessYabaiClient


class CollapseCurrentSpaceTests(unittest.TestCase):
    def test_collapse_current_space_stacks_other_eligible_windows_and_records_background_pool(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            client = FakeYabaiClient(
                focused_window=eligible_window(101),
                space_windows=[
                    eligible_window(101),
                    eligible_window(102),
                    eligible_window(103),
                    eligible_window(201, **{"is-floating": True}),
                    eligible_window(202, **{"is-hidden": True}),
                    eligible_window(203, **{"is-minimized": True}),
                    eligible_window(204, subrole="AXDialog"),
                    eligible_window(205, display=2),
                ],
            )

            result = CollapseCurrentSpaceService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
            ).run()

            self.assertEqual(result.focused_window_id, 101)
            self.assertEqual(result.background_window_ids, [102, 103])
            self.assertEqual(
                client.actions,
                [
                    ("set_layout", 2, "bsp"),
                    ("stack", 101, 102),
                    ("stack", 101, 103),
                    ("focus", 101),
                ],
            )

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(
                payload["spaces"]["1:2"]["background_window_ids"],
                [102, 103],
            )
            self.assertEqual(payload["spaces"]["1:2"]["visible_window_id"], 101)

    def test_single_eligible_window_keeps_empty_background_pool(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            client = FakeYabaiClient(
                focused_window=eligible_window(101),
                space_windows=[
                    eligible_window(101),
                    eligible_window(900, **{"is-hidden": True}),
                ],
            )

            result = CollapseCurrentSpaceService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
            ).run()

            self.assertEqual(result.background_window_ids, [])
            self.assertEqual(
                client.actions,
                [
                    ("set_layout", 2, "bsp"),
                    ("focus", 101),
                ],
            )

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["spaces"]["1:2"]["background_window_ids"], [])

    def test_incompatible_environment_fails_without_mutation_or_state_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            client = FakeYabaiClient(
                focused_window=eligible_window(101),
                space_windows=[eligible_window(101), eligible_window(102)],
                focus_follows_mouse="autofocus",
            )

            with self.assertRaisesRegex(WorkflowError, "focus_follows_mouse"):
                CollapseCurrentSpaceService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                ).run()

            self.assertEqual(client.actions, [])
            self.assertFalse(state_path.exists())

    def test_layout_failure_does_not_commit_background_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            client = FakeYabaiClient(
                focused_window=eligible_window(101),
                space_windows=[eligible_window(101), eligible_window(102)],
                fail_on_layout=True,
            )

            with self.assertRaisesRegex(WorkflowError, "Failed to switch target space"):
                CollapseCurrentSpaceService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                ).run()

            self.assertEqual(client.actions, [("set_layout", 2, "bsp")])
            self.assertFalse(state_path.exists())

    def test_focus_follows_mouse_disabled_alias_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            client = FakeYabaiClient(
                focused_window=eligible_window(101),
                space_windows=[eligible_window(101), eligible_window(102)],
                focus_follows_mouse="disabled",
            )

            result = CollapseCurrentSpaceService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
            ).run()

            self.assertEqual(result.background_window_ids, [102])
            self.assertEqual(
                client.actions,
                [
                    ("set_layout", 2, "bsp"),
                    ("stack", 101, 102),
                    ("focus", 101),
                ],
            )

    def test_non_standard_window_level_is_filtered_out_of_the_background_pool(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            client = FakeYabaiClient(
                focused_window=eligible_window(101),
                space_windows=[
                    eligible_window(101),
                    eligible_window(102),
                    eligible_window(400, layer="above", level=3),
                    eligible_window(401, layer="unknown", level=7),
                ],
            )

            result = CollapseCurrentSpaceService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
            ).run()

            self.assertEqual(result.background_window_ids, [102])
            self.assertEqual(
                client.actions,
                [
                    ("set_layout", 2, "bsp"),
                    ("stack", 101, 102),
                    ("focus", 101),
                ],
            )

    def test_invalid_persisted_state_blocks_mutation_before_collapse_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            state_path.write_text("{ invalid json\n", encoding="utf-8")
            client = FakeYabaiClient(
                focused_window=eligible_window(101),
                space_windows=[eligible_window(101), eligible_window(102)],
            )

            with self.assertRaisesRegex(WorkflowError, "not valid JSON"):
                CollapseCurrentSpaceService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                ).run()

            self.assertEqual(client.actions, [])
            self.assertEqual(
                state_path.read_text(encoding="utf-8"),
                "{ invalid json\n",
            )

    def test_schema_invalid_list_state_blocks_mutation_before_collapse_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            state_path.write_text("[]\n", encoding="utf-8")
            client = FakeYabaiClient(
                focused_window=eligible_window(101),
                space_windows=[eligible_window(101), eligible_window(102)],
            )

            with self.assertRaisesRegex(WorkflowError, "invalid schema"):
                CollapseCurrentSpaceService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                ).run()

            self.assertEqual(client.actions, [])
            self.assertEqual(state_path.read_text(encoding="utf-8"), "[]\n")

    def test_schema_invalid_spaces_array_blocks_mutation_before_collapse_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            state_path.write_text('{"spaces":[]}\n', encoding="utf-8")
            client = FakeYabaiClient(
                focused_window=eligible_window(101),
                space_windows=[eligible_window(101), eligible_window(102)],
            )

            with self.assertRaisesRegex(WorkflowError, "invalid schema"):
                CollapseCurrentSpaceService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                ).run()

            self.assertEqual(client.actions, [])
            self.assertEqual(
                state_path.read_text(encoding="utf-8"),
                '{"spaces":[]}\n',
            )


class SplitFromBackgroundPoolTests(unittest.TestCase):
    def test_split_from_background_pool_promotes_one_background_window_and_updates_pool(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                background_window_ids=[102, 103],
                visible_window_id=101,
            )
            client = FakeYabaiClient(
                focused_window=eligible_window(101),
                space_windows=[
                    eligible_window(101),
                    eligible_window(102),
                    eligible_window(103),
                ],
            )

            result = SplitFromBackgroundPoolService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
            ).run()

            self.assertEqual(result.promoted_window_id, 102)
            self.assertEqual(result.background_window_ids, [103])
            self.assertIsNone(result.pending_split_direction)
            self.assertEqual(
                client.actions,
                [
                    ("promote_stacked", 102, DEFAULT_PENDING_SPLIT_DIRECTION),
                    ("focus", 101),
                ],
            )

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["spaces"]["1:2"]["background_window_ids"], [103])
            self.assertEqual(payload["spaces"]["1:2"]["visible_window_id"], 101)

    def test_missing_background_entry_arms_native_pending_split_without_state_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            client = FakeYabaiClient(
                focused_window=eligible_window(101),
                space_windows=[eligible_window(101)],
            )

            result = SplitFromBackgroundPoolService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
            ).run()

            self.assertIsNone(result.promoted_window_id)
            self.assertEqual(result.background_window_ids, [])
            self.assertEqual(
                result.pending_split_direction,
                DEFAULT_PENDING_SPLIT_DIRECTION,
            )
            self.assertEqual(
                client.actions,
                [("insert", 101, DEFAULT_PENDING_SPLIT_DIRECTION)],
            )
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["spaces"]["1:2"]["background_window_ids"], [])
            self.assertEqual(
                payload["spaces"]["1:2"]["pending_split_direction"],
                DEFAULT_PENDING_SPLIT_DIRECTION,
            )
            self.assertEqual(payload["spaces"]["1:2"]["visible_window_id"], 101)

    def test_split_ignores_stale_and_ineligible_background_ids_and_refreshes_pool(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                background_window_ids=[900, 901, 102, 103],
                visible_window_id=101,
            )
            client = FakeYabaiClient(
                focused_window=eligible_window(101),
                space_windows=[
                    eligible_window(101),
                    eligible_window(102),
                    eligible_window(103),
                    eligible_window(900, **{"is-hidden": True}),
                    eligible_window(901, display=2),
                ],
            )

            result = SplitFromBackgroundPoolService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
            ).run()

            self.assertEqual(result.promoted_window_id, 102)
            self.assertEqual(result.background_window_ids, [103])
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["spaces"]["1:2"]["background_window_ids"], [103])

    def test_invalid_background_pool_entry_blocks_mutation_before_split_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "spaces": {
                            "1:2": {
                                "display": 1,
                                "space": 2,
                                "visible_window_id": 101,
                                "background_window_ids": ["102"],
                            }
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            client = FakeYabaiClient(
                focused_window=eligible_window(101),
                space_windows=[eligible_window(101), eligible_window(102)],
            )

            with self.assertRaisesRegex(WorkflowError, "invalid schema"):
                SplitFromBackgroundPoolService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                ).run()

            self.assertEqual(client.actions, [])
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["spaces"]["1:2"]["background_window_ids"],
                ["102"],
            )

    def test_split_promotion_failure_does_not_commit_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            write_state_entry(
                state_path,
                background_window_ids=[102, 103],
                visible_window_id=101,
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeYabaiClient(
                focused_window=eligible_window(101),
                space_windows=[
                    eligible_window(101),
                    eligible_window(102),
                    eligible_window(103),
                ],
                fail_on_promote=True,
            )

            with self.assertRaisesRegex(WorkflowError, "Failed to promote stacked window"):
                SplitFromBackgroundPoolService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                ).run()

            self.assertEqual(
                client.actions,
                [("promote_stacked", 102, DEFAULT_PENDING_SPLIT_DIRECTION)],
            )
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)

    def test_pending_split_failure_does_not_commit_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            client = FakeYabaiClient(
                focused_window=eligible_window(101),
                space_windows=[eligible_window(101)],
                fail_on_insert=True,
            )

            with self.assertRaisesRegex(
                WorkflowError,
                "Failed to arm a pending east split",
            ):
                SplitFromBackgroundPoolService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                ).run()

            self.assertEqual(
                client.actions,
                [("insert", 101, DEFAULT_PENDING_SPLIT_DIRECTION)],
            )
            self.assertFalse(state_path.exists())


class FakeYabaiClient:
    def __init__(
        self,
        *,
        focused_window: dict,
        space_windows: list[dict],
        focus_follows_mouse: str = "off",
        mouse_follows_focus: str = "off",
        space_layout: str = "bsp",
        fail_on_layout: bool = False,
        fail_on_promote: bool = False,
        fail_on_insert: bool = False,
    ) -> None:
        self.focused_window = focused_window
        self.space_windows = space_windows
        self.focus_follows_mouse = focus_follows_mouse
        self.mouse_follows_focus = mouse_follows_focus
        self.space_layout = space_layout
        self.fail_on_layout = fail_on_layout
        self.fail_on_promote = fail_on_promote
        self.fail_on_insert = fail_on_insert
        self.actions: list[tuple] = []

    def get_config(self, setting: str, *, space: Optional[int] = None) -> str:
        if setting == "focus_follows_mouse":
            return self.focus_follows_mouse
        if setting == "mouse_follows_focus":
            return self.mouse_follows_focus
        if setting == "layout":
            return self.space_layout
        raise AssertionError(f"Unexpected config request: {setting}")

    def query_focused_window(self) -> dict:
        return self.focused_window

    def query_display(self, display: int) -> dict:
        return {
            "index": display,
            "has-focus": True,
            "spaces": [2],
        }

    def query_space(self, space: int) -> dict:
        return {
            "index": space,
            "display": 1,
            "windows": [window["id"] for window in self.space_windows],
            "is-visible": True,
            "is-native-fullscreen": False,
        }

    def query_windows_for_space(self, space: int) -> list[dict]:
        return list(self.space_windows)

    def set_space_layout(self, space: int, layout: str) -> None:
        self.actions.append(("set_layout", space, layout))
        if self.fail_on_layout:
            raise WorkflowError(f"Failed to switch target space {space} to {layout}")

    def stack_window(self, anchor_window_id: int, candidate_window_id: int) -> None:
        self.actions.append(("stack", anchor_window_id, candidate_window_id))

    def promote_stacked_window(self, window_id: int, direction: str) -> None:
        self.actions.append(("promote_stacked", window_id, direction))
        if self.fail_on_promote:
            raise WorkflowError(
                "Failed to promote stacked window "
                f"{window_id} into a sibling {direction} split"
            )

    def arm_window_split(self, window_id: int, direction: str) -> None:
        self.actions.append(("insert", window_id, direction))
        if self.fail_on_insert:
            raise WorkflowError(
                f"Failed to arm a pending {direction} split on window {window_id}"
            )

    def focus_window(self, window_id: int) -> None:
        self.actions.append(("focus", window_id))


class SubprocessYabaiClientTests(unittest.TestCase):
    def test_promote_stacked_window_uses_insert_and_double_float_toggle(self) -> None:
        client = SubprocessYabaiClient(yabai_bin="/opt/homebrew/bin/yabai")
        calls: list[list[str]] = []

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("runtime.yhwm.yabai.subprocess.run", side_effect=fake_run):
            client.promote_stacked_window(102, DEFAULT_PENDING_SPLIT_DIRECTION)

        self.assertEqual(
            calls,
            [
                [
                    "/opt/homebrew/bin/yabai",
                    "-m",
                    "window",
                    "102",
                    "--insert",
                    DEFAULT_PENDING_SPLIT_DIRECTION,
                    "--toggle",
                    "float",
                    "--toggle",
                    "float",
                ]
            ],
        )


def write_state_entry(
    path: Path,
    *,
    background_window_ids: list[int],
    visible_window_id: int,
    pending_split_direction: Optional[str] = None,
) -> None:
    entry = {
        "display": 1,
        "space": 2,
        "visible_window_id": visible_window_id,
        "background_window_ids": background_window_ids,
    }
    if pending_split_direction is not None:
        entry["pending_split_direction"] = pending_split_direction

    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "spaces": {
                    "1:2": entry
                },
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
