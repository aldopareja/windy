from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest

from runtime.yhwm.alttab_session import (
    AltTabSelectedWindowService,
    AltTabSessionArmService,
    AltTabSessionCancelService,
    SUPPORTED_ALTTAB_CANCEL_REASONS,
)
from runtime.yhwm.cli import (
    alttab_session_cancel_main,
    alttab_session_selected_window_main,
)
from runtime.yhwm.errors import WorkflowError
from runtime.yhwm.models import ArmedAltTabSession
from runtime.yhwm.state import AltTabSessionStore, WorkflowStateStore
from runtime.yhwm.window_focused import WindowFocusedService


class AltTabSessionArmServiceTests(unittest.TestCase):
    def test_chooser_open_arms_one_session_without_mutating_workflow_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            session_path = Path(tempdir) / "alttab_session.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102, 103],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeAltTabYabaiClient(
                focused_window_id=101,
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102),
                    103: eligible_window(103),
                },
                space_windows={
                    2: [101, 102, 103],
                },
            )

            result = AltTabSessionArmService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                session_store=AltTabSessionStore(session_path),
            ).run()

            self.assertEqual(result.action, "armed_session")
            self.assertEqual(result.workflow_space.storage_key, "1:2")
            self.assertEqual(result.origin_window_id, 101)
            self.assertTrue(result.session_active)
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)

            payload = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["session"]["origin_window_id"], 101)
            self.assertEqual(payload["session"]["origin_display"], 1)
            self.assertEqual(payload["session"]["origin_space"], 2)

    def test_background_window_origin_does_not_arm_session(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            session_path = Path(tempdir) / "alttab_session.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102, 103],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeAltTabYabaiClient(
                focused_window_id=102,
                window_records={
                    101: eligible_window(101),
                    102: eligible_window(102, **{"has-focus": True}),
                    103: eligible_window(103),
                },
                space_windows={
                    2: [101, 102, 103],
                },
            )

            result = AltTabSessionArmService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                session_store=AltTabSessionStore(session_path),
            ).run()

            self.assertEqual(result.action, "ignored_background_window")
            self.assertFalse(result.session_active)
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)
            self.assertIsNone(AltTabSessionStore(session_path).read_session())

    def test_untracked_space_does_not_arm_session(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            session_path = Path(tempdir) / "alttab_session.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeAltTabYabaiClient(
                focused_window_id=201,
                window_records={
                    101: eligible_window(101),
                    102: eligible_window(102),
                    201: eligible_window(201, display=2, space=5, **{"has-focus": True}),
                },
                space_windows={
                    2: [101, 102],
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

            result = AltTabSessionArmService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                session_store=AltTabSessionStore(session_path),
            ).run()

            self.assertEqual(result.action, "ignored_untracked_space")
            self.assertFalse(result.session_active)
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)
            self.assertIsNone(AltTabSessionStore(session_path).read_session())

    def test_existing_session_blocks_second_arm(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            session_path = Path(tempdir) / "alttab_session.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102],
            )
            session_store = AltTabSessionStore(session_path)
            session_store.arm_session(
                ArmedAltTabSession(
                    origin_window_id=101,
                    origin_workflow_space=workflow_space(1, 2),
                )
            )
            original_state = state_path.read_text(encoding="utf-8")
            original_session = session_path.read_text(encoding="utf-8")
            client = FakeAltTabYabaiClient(
                focused_window_id=101,
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102),
                },
                space_windows={
                    2: [101, 102],
                },
            )

            result = AltTabSessionArmService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                session_store=session_store,
            ).run()

            self.assertEqual(result.action, "ignored_existing_session")
            self.assertTrue(result.session_active)
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)
            self.assertEqual(session_path.read_text(encoding="utf-8"), original_session)

    def test_invalid_workflow_state_blocks_arm_without_partial_session_write(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            session_path = Path(tempdir) / "alttab_session.json"
            state_path.write_text(
                '{"schema_version":1,"spaces":{"1:2":{"display":1,"space":2,"visible_window_id":101,"background_window_ids":[102],"pending_split_direction":[]}}}\n',
                encoding="utf-8",
            )
            client = FakeAltTabYabaiClient(
                focused_window_id=101,
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                },
                space_windows={
                    2: [101],
                },
            )

            with self.assertRaisesRegex(WorkflowError, "invalid schema"):
                AltTabSessionArmService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                    session_store=AltTabSessionStore(session_path),
                ).run()

            self.assertFalse(session_path.exists())

    def test_origin_space_must_be_visible_bsp_to_arm_session(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            session_path = Path(tempdir) / "alttab_session.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeAltTabYabaiClient(
                focused_window_id=101,
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102),
                },
                space_windows={
                    2: [101, 102],
                },
                space_layouts={2: "stack"},
            )

            with self.assertRaisesRegex(WorkflowError, "must use layout 'bsp'"):
                AltTabSessionArmService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                    session_store=AltTabSessionStore(session_path),
                ).run()

            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)
            self.assertFalse(session_path.exists())


class AltTabSessionCancelServiceTests(unittest.TestCase):
    def test_explicit_cancel_reasons_disarm_without_mutating_workflow_state(self) -> None:
        for reason in sorted(SUPPORTED_ALTTAB_CANCEL_REASONS):
            with self.subTest(reason=reason):
                with tempfile.TemporaryDirectory() as tempdir:
                    state_path = Path(tempdir) / "workflow_state.json"
                    session_path = Path(tempdir) / "alttab_session.json"
                    write_state_entry(
                        state_path,
                        visible_window_id=101,
                        background_window_ids=[102, 103],
                    )
                    original_state = state_path.read_text(encoding="utf-8")
                    session_store = AltTabSessionStore(session_path)
                    session_store.arm_session(
                        ArmedAltTabSession(
                            origin_window_id=101,
                            origin_workflow_space=workflow_space(1, 2),
                        )
                    )

                    result = AltTabSessionCancelService(
                        session_store=session_store,
                        reason=reason,
                    ).run()

                    self.assertEqual(result.action, "canceled_session")
                    self.assertFalse(result.session_active)
                    self.assertEqual(result.origin_window_id, 101)
                    self.assertEqual(result.workflow_space.storage_key, "1:2")
                    self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)
                    self.assertIsNone(session_store.read_session())

    def test_cancel_without_armed_session_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            session_path = Path(tempdir) / "alttab_session.json"
            result = AltTabSessionCancelService(
                session_store=AltTabSessionStore(session_path),
                reason="esc",
            ).run()

            self.assertEqual(result.action, "ignored_no_armed_session")
            self.assertFalse(result.session_active)


class AltTabSelectedWindowServiceTests(unittest.TestCase):
    def test_other_space_selection_cancels_session_without_mutating_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            session_path = Path(tempdir) / "alttab_session.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102, 103],
            )
            original_state = state_path.read_text(encoding="utf-8")
            session_store = AltTabSessionStore(session_path)
            session_store.arm_session(
                ArmedAltTabSession(
                    origin_window_id=101,
                    origin_workflow_space=workflow_space(1, 2),
                )
            )
            client = FakeAltTabYabaiClient(
                focused_window_id=101,
                window_records={
                    301: eligible_window(301, display=1, space=5),
                },
                space_windows={
                    2: [101, 102, 103],
                    5: [301],
                },
                display_spaces={
                    1: [2, 5],
                },
            )

            result = AltTabSelectedWindowService(
                yabai=client,
                session_store=session_store,
                selected_window_id=301,
            ).run()

            self.assertEqual(result.action, "canceled_cross_space_or_display_selection")
            self.assertFalse(result.session_active)
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)
            self.assertIsNone(session_store.read_session())

    def test_other_display_selection_cancels_session_without_mutating_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            session_path = Path(tempdir) / "alttab_session.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102],
            )
            original_state = state_path.read_text(encoding="utf-8")
            session_store = AltTabSessionStore(session_path)
            session_store.arm_session(
                ArmedAltTabSession(
                    origin_window_id=101,
                    origin_workflow_space=workflow_space(1, 2),
                )
            )
            client = FakeAltTabYabaiClient(
                focused_window_id=101,
                window_records={
                    401: eligible_window(401, display=2, space=5),
                },
                space_windows={
                    2: [101, 102],
                    5: [401],
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

            result = AltTabSelectedWindowService(
                yabai=client,
                session_store=session_store,
                selected_window_id=401,
            ).run()

            self.assertEqual(result.action, "canceled_cross_space_or_display_selection")
            self.assertFalse(result.session_active)
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)
            self.assertIsNone(session_store.read_session())

    def test_same_space_selection_keeps_session_armed_without_state_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            session_path = Path(tempdir) / "alttab_session.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102],
            )
            original_state = state_path.read_text(encoding="utf-8")
            session_store = AltTabSessionStore(session_path)
            session_store.arm_session(
                ArmedAltTabSession(
                    origin_window_id=101,
                    origin_workflow_space=workflow_space(1, 2),
                )
            )
            client = FakeAltTabYabaiClient(
                focused_window_id=101,
                window_records={
                    201: eligible_window(201),
                },
                space_windows={
                    2: [101, 102, 201],
                },
            )

            result = AltTabSelectedWindowService(
                yabai=client,
                session_store=session_store,
                selected_window_id=201,
            ).run()

            self.assertEqual(result.action, "ignored_same_origin_space_selection")
            self.assertTrue(result.session_active)
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)
            self.assertIsNotNone(session_store.read_session())

    def test_selected_window_query_failure_keeps_session_armed(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            session_path = Path(tempdir) / "alttab_session.json"
            session_store = AltTabSessionStore(session_path)
            session_store.arm_session(
                ArmedAltTabSession(
                    origin_window_id=101,
                    origin_workflow_space=workflow_space(1, 2),
                )
            )
            client = FakeAltTabYabaiClient(
                focused_window_id=101,
                window_records={
                    301: eligible_window(301, display=1, space=5),
                },
                space_windows={
                    5: [301],
                },
                fail_on_query_window_id=301,
            )

            with self.assertRaisesRegex(WorkflowError, "Failed to query window 301"):
                AltTabSelectedWindowService(
                    yabai=client,
                    session_store=session_store,
                    selected_window_id=301,
                ).run()

            self.assertIsNotNone(session_store.read_session())


class WindowFocusedAltTabGuardTests(unittest.TestCase):
    def test_window_focused_is_ignored_while_alttab_session_is_armed(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            session_path = Path(tempdir) / "alttab_session.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[103],
            )
            original_state = state_path.read_text(encoding="utf-8")
            session_store = AltTabSessionStore(session_path)
            session_store.arm_session(
                ArmedAltTabSession(
                    origin_window_id=101,
                    origin_workflow_space=workflow_space(1, 2),
                )
            )
            client = FakeAltTabYabaiClient(
                focused_window_id=102,
                window_records={
                    101: eligible_window(101),
                    102: eligible_window(102, **{"has-focus": True}),
                    103: eligible_window(103),
                },
                space_windows={
                    2: [101, 102, 103],
                },
            )

            result = WindowFocusedService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                alttab_session_store=session_store,
                window_id=102,
            ).run()

            self.assertEqual(result.action, "ignored_armed_alttab_session")
            self.assertEqual(result.workflow_space.storage_key, "1:2")
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)


class AltTabSessionCliTests(unittest.TestCase):
    def test_cancel_requires_reason(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = alttab_session_cancel_main(
                [
                    "--state-file",
                    "/tmp/unused-state.json",
                    "--alttab-session-file",
                    "/tmp/unused-session.json",
                    "--yabai-bin",
                    "/usr/bin/false",
                ]
            )

        self.assertEqual(exit_code, 1)

    def test_cancel_rejects_unsupported_reason(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = alttab_session_cancel_main(
                [
                    "--reason",
                    "modifier_release",
                    "--state-file",
                    "/tmp/unused-state.json",
                    "--alttab-session-file",
                    "/tmp/unused-session.json",
                    "--yabai-bin",
                    "/usr/bin/false",
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("unsupported reason", stderr.getvalue())

    def test_selected_window_requires_window_id(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = alttab_session_selected_window_main(
                [
                    "--window-id",
                    "",
                    "--state-file",
                    "/tmp/unused-state.json",
                    "--alttab-session-file",
                    "/tmp/unused-session.json",
                    "--yabai-bin",
                    "/usr/bin/false",
                ]
            )

        self.assertEqual(exit_code, 1)


class FakeAltTabYabaiClient:
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
        raise AssertionError("query_recent_window should not be used in AltTab session tests")

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
        raise AssertionError("set_space_layout should not be used in AltTab session tests")

    def stack_window(self, anchor_window_id: int, candidate_window_id: int) -> None:
        raise AssertionError("stack_window should not be used in AltTab session tests")

    def promote_stacked_window(self, window_id: int, direction: str) -> None:
        raise AssertionError(
            "promote_stacked_window should not be used in AltTab session tests"
        )

    def arm_window_split(self, window_id: int, direction: str) -> None:
        raise AssertionError(
            "arm_window_split should not be used in AltTab session tests"
        )

    def focus_window(self, window_id: int) -> None:
        raise AssertionError("focus_window should not be used in AltTab session tests")


def workflow_space(display: int, space: int) -> object:
    from runtime.yhwm.models import EligibleWorkflowSpace

    return EligibleWorkflowSpace(display=display, space=space)


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
