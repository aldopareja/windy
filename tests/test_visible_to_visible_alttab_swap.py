from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from runtime.yhwm.alttab_session import (
    AltTabModifierReleaseService,
    AltTabSelectedWindowService,
    AltTabSessionArmService,
)
from runtime.yhwm.errors import WorkflowError
from runtime.yhwm.state import AltTabSessionStore, WorkflowStateStore


class AltTabVisibleToVisibleSwapTests(unittest.TestCase):
    def test_modifier_release_swaps_selected_visible_window_and_updates_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            session_path = Path(tempdir) / "alttab_session.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102],
                pending_split_direction="east",
            )
            client = FakeAltTabSwapYabaiClient(
                focused_window_id=101,
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102),
                    201: eligible_window(201),
                },
                space_windows={2: [101, 102, 201]},
            )
            session_store = AltTabSessionStore(session_path)

            arm_result = AltTabSessionArmService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                session_store=session_store,
            ).run()
            self.assertEqual(arm_result.action, "armed_session")
            self.assertEqual(session_store.read_session().selected_window_id, 101)

            selected_result = AltTabSelectedWindowService(
                yabai=client,
                session_store=session_store,
                selected_window_id=201,
            ).run()
            self.assertEqual(selected_result.action, "ignored_same_origin_space_selection")
            self.assertEqual(session_store.read_session().selected_window_id, 201)

            release_result = AltTabModifierReleaseService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                session_store=session_store,
            ).run()

            self.assertEqual(release_result.action, "committed_visible_window_swap")
            self.assertEqual(release_result.visible_window_id, 201)
            self.assertEqual(release_result.background_window_ids, [102])
            self.assertEqual(release_result.pending_split_direction, "east")
            self.assertEqual(client.actions, [("swap", 101, 201), ("focus", 201)])
            self.assertIsNone(session_store.read_session())

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["spaces"]["1:2"]["visible_window_id"], 201)
            self.assertEqual(payload["spaces"]["1:2"]["background_window_ids"], [102])
            self.assertEqual(payload["spaces"]["1:2"]["pending_split_direction"], "east")

    def test_latest_same_space_selection_wins_on_modifier_release(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            session_path = Path(tempdir) / "alttab_session.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeAltTabSwapYabaiClient(
                focused_window_id=101,
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102),
                    201: eligible_window(201),
                    202: eligible_window(202),
                },
                space_windows={2: [101, 102, 201, 202]},
            )
            session_store = AltTabSessionStore(session_path)

            AltTabSessionArmService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                session_store=session_store,
            ).run()
            AltTabSelectedWindowService(
                yabai=client,
                session_store=session_store,
                selected_window_id=201,
            ).run()
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)
            AltTabSelectedWindowService(
                yabai=client,
                session_store=session_store,
                selected_window_id=202,
            ).run()
            self.assertEqual(session_store.read_session().selected_window_id, 202)

            release_result = AltTabModifierReleaseService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                session_store=session_store,
            ).run()

            self.assertEqual(release_result.action, "committed_visible_window_swap")
            self.assertEqual(release_result.selected_window_id, 202)
            self.assertEqual(client.actions, [("swap", 101, 202), ("focus", 202)])

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["spaces"]["1:2"]["visible_window_id"], 202)

    def test_modifier_release_with_origin_still_selected_cancels_without_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            session_path = Path(tempdir) / "alttab_session.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeAltTabSwapYabaiClient(
                focused_window_id=101,
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102),
                    201: eligible_window(201),
                },
                space_windows={2: [101, 102, 201]},
            )
            session_store = AltTabSessionStore(session_path)

            AltTabSessionArmService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                session_store=session_store,
            ).run()

            release_result = AltTabModifierReleaseService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                session_store=session_store,
            ).run()

            self.assertEqual(release_result.action, "canceled_origin_still_selected")
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)
            self.assertEqual(client.actions, [])
            self.assertIsNone(session_store.read_session())

    def test_modifier_release_with_background_selection_is_canceled_without_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            session_path = Path(tempdir) / "alttab_session.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeAltTabSwapYabaiClient(
                focused_window_id=101,
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102),
                    201: eligible_window(201),
                },
                space_windows={2: [101, 102, 201]},
            )
            session_store = AltTabSessionStore(session_path)

            AltTabSessionArmService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                session_store=session_store,
            ).run()
            AltTabSelectedWindowService(
                yabai=client,
                session_store=session_store,
                selected_window_id=102,
            ).run()

            release_result = AltTabModifierReleaseService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                session_store=session_store,
            ).run()

            self.assertEqual(
                release_result.action,
                "canceled_background_window_selection",
            )
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)
            self.assertEqual(client.actions, [])
            self.assertIsNone(session_store.read_session())

    def test_modifier_release_without_armed_session_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            session_path = Path(tempdir) / "alttab_session.json"
            client = FakeAltTabSwapYabaiClient(
                focused_window_id=101,
                window_records={101: eligible_window(101, **{"has-focus": True})},
                space_windows={2: [101]},
            )

            result = AltTabModifierReleaseService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                session_store=AltTabSessionStore(session_path),
            ).run()

            self.assertEqual(result.action, "ignored_no_armed_session")
            self.assertEqual(client.actions, [])

    def test_swap_failure_does_not_write_partial_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            session_path = Path(tempdir) / "alttab_session.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeAltTabSwapYabaiClient(
                focused_window_id=101,
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102),
                    201: eligible_window(201),
                },
                space_windows={2: [101, 102, 201]},
                fail_on_swap=True,
            )
            session_store = AltTabSessionStore(session_path)

            AltTabSessionArmService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                session_store=session_store,
            ).run()
            AltTabSelectedWindowService(
                yabai=client,
                session_store=session_store,
                selected_window_id=201,
            ).run()

            with self.assertRaisesRegex(WorkflowError, "Failed to swap visible workflow windows"):
                AltTabModifierReleaseService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                    session_store=session_store,
                ).run()

            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)
            self.assertEqual(client.actions, [("swap", 101, 201)])
            self.assertEqual(session_store.read_session().selected_window_id, 201)


class FakeAltTabSwapYabaiClient:
    def __init__(
        self,
        *,
        focused_window_id: int,
        window_records: dict[int, dict],
        space_windows: dict[int, list[int]],
        display_spaces: dict[int, list[int]] | None = None,
        space_displays: dict[int, int] | None = None,
        focus_follows_mouse: str = "off",
        mouse_follows_focus: str = "off",
        space_layouts: dict[int, str] | None = None,
        fail_on_query_window_id: int | None = None,
        fail_on_swap: bool = False,
    ) -> None:
        self.focused_window_id = focused_window_id
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
        self.fail_on_query_window_id = fail_on_query_window_id
        self.fail_on_swap = fail_on_swap
        self.actions: list[tuple[object, ...]] = []

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
        return dict(self.window_records[self.focused_window_id])

    def query_window(self, window_id: int) -> dict:
        if self.fail_on_query_window_id == window_id:
            raise WorkflowError(f"Failed to query window {window_id} from yabai")
        return dict(self.window_records[window_id])

    def query_recent_window(self) -> dict:
        raise AssertionError(
            "query_recent_window should not be used in visible AltTab swap tests"
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
            "is-visible": True,
            "is-native-fullscreen": False,
        }

    def query_windows_for_space(self, space: int) -> list[dict]:
        return [
            dict(self.window_records[window_id])
            for window_id in self.space_windows.get(space, [])
        ]

    def set_space_layout(self, space: int, layout: str) -> None:
        raise AssertionError("set_space_layout should not be used in AltTab swap tests")

    def stack_window(self, anchor_window_id: int, candidate_window_id: int) -> None:
        raise AssertionError("stack_window should not be used in AltTab swap tests")

    def promote_stacked_window(self, window_id: int, direction: str) -> None:
        raise AssertionError(
            "promote_stacked_window should not be used in AltTab swap tests"
        )

    def arm_window_split(self, window_id: int, direction: str) -> None:
        raise AssertionError(
            "arm_window_split should not be used in AltTab swap tests"
        )

    def focus_window(self, window_id: int) -> None:
        self.actions.append(("focus", window_id))
        self.focused_window_id = window_id

    def swap_window(self, window_id: int, target_window_id: int) -> None:
        self.actions.append(("swap", window_id, target_window_id))
        if self.fail_on_swap:
            raise WorkflowError(
                "Failed to swap visible workflow windows "
                f"{window_id} and {target_window_id}"
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

    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "spaces": {f"{display}:{space}": entry},
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
