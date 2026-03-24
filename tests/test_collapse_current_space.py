from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Optional
import unittest

from runtime.yhwm.collapse import CollapseCurrentSpaceService
from runtime.yhwm.errors import WorkflowError
from runtime.yhwm.state import WorkflowStateStore


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
    ) -> None:
        self.focused_window = focused_window
        self.space_windows = space_windows
        self.focus_follows_mouse = focus_follows_mouse
        self.mouse_follows_focus = mouse_follows_focus
        self.space_layout = space_layout
        self.fail_on_layout = fail_on_layout
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

    def focus_window(self, window_id: int) -> None:
        self.actions.append(("focus", window_id))


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
