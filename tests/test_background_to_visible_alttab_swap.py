from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from runtime.tests.test_visible_to_visible_alttab_swap import (
    FakeAltTabSwapYabaiClient,
    eligible_window,
    write_state_entry,
)
from runtime.yhwm.alttab_session import (
    AltTabModifierReleaseService,
    AltTabSelectedWindowService,
    AltTabSessionArmService,
)
from runtime.yhwm.errors import WorkflowError
from runtime.yhwm.state import AltTabSessionStore, WorkflowStateStore


class AltTabBackgroundToVisibleSwapTests(unittest.TestCase):
    def test_modifier_release_replaces_visible_with_selected_background_window(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            session_path = Path(tempdir) / "alttab_session.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102, 103],
            )
            client = FakeAltTabSwapYabaiClient(
                focused_window_id=101,
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102),
                    103: eligible_window(103),
                },
                space_windows={2: [101, 102, 103]},
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
                "committed_background_window_replacement",
            )
            self.assertEqual(release_result.visible_window_id, 102)
            self.assertCountEqual(release_result.background_window_ids, [101, 103])
            self.assertEqual(client.actions, [("focus", 102)])
            self.assertIsNone(session_store.read_session())

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["spaces"]["1:2"]["visible_window_id"], 102)
            self.assertCountEqual(
                payload["spaces"]["1:2"]["background_window_ids"],
                [101, 103],
            )

    def test_latest_background_selection_wins_on_modifier_release(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            session_path = Path(tempdir) / "alttab_session.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102, 103],
            )
            client = FakeAltTabSwapYabaiClient(
                focused_window_id=101,
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102),
                    103: eligible_window(103),
                },
                space_windows={2: [101, 102, 103]},
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
            AltTabSelectedWindowService(
                yabai=client,
                session_store=session_store,
                selected_window_id=103,
            ).run()

            release_result = AltTabModifierReleaseService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                session_store=session_store,
            ).run()

            self.assertEqual(
                release_result.action,
                "committed_background_window_replacement",
            )
            self.assertEqual(release_result.selected_window_id, 103)
            self.assertCountEqual(release_result.background_window_ids, [101, 102])
            self.assertEqual(client.actions, [("focus", 103)])

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["spaces"]["1:2"]["visible_window_id"], 103)
            self.assertCountEqual(
                payload["spaces"]["1:2"]["background_window_ids"],
                [101, 102],
            )

    def test_background_replacement_preserves_pending_split_direction(self) -> None:
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
                },
                space_windows={2: [101, 102]},
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

            self.assertEqual(release_result.visible_window_id, 102)
            self.assertEqual(release_result.pending_split_direction, "east")
            self.assertEqual(release_result.background_window_ids, [101])

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["spaces"]["1:2"]["visible_window_id"], 102)
            self.assertEqual(payload["spaces"]["1:2"]["background_window_ids"], [101])
            self.assertEqual(payload["spaces"]["1:2"]["pending_split_direction"], "east")

    def test_non_eligible_remembered_selected_window_is_canceled_without_mutation(
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
                },
                space_windows={2: [101, 102]},
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

            client.window_records[102]["is-hidden"] = True

            release_result = AltTabModifierReleaseService(
                yabai=client,
                state_store=WorkflowStateStore(state_path),
                session_store=session_store,
            ).run()

            self.assertEqual(
                release_result.action,
                "canceled_ineligible_selected_window",
            )
            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)
            self.assertEqual(client.actions, [])
            self.assertIsNone(session_store.read_session())

    def test_background_replacement_failure_does_not_write_partial_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "workflow_state.json"
            session_path = Path(tempdir) / "alttab_session.json"
            write_state_entry(
                state_path,
                visible_window_id=101,
                background_window_ids=[102, 103],
            )
            original_state = state_path.read_text(encoding="utf-8")
            client = FakeAltTabSwapYabaiClient(
                focused_window_id=101,
                window_records={
                    101: eligible_window(101, **{"has-focus": True}),
                    102: eligible_window(102),
                    103: eligible_window(103),
                },
                space_windows={2: [101, 102, 103]},
                fail_on_focus=True,
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

            with self.assertRaisesRegex(
                WorkflowError,
                "Failed to refocus window 102 after workflow mutation",
            ):
                AltTabModifierReleaseService(
                    yabai=client,
                    state_store=WorkflowStateStore(state_path),
                    session_store=session_store,
                ).run()

            self.assertEqual(state_path.read_text(encoding="utf-8"), original_state)
            self.assertEqual(client.actions, [("focus", 102)])
            self.assertEqual(session_store.read_session().selected_window_id, 102)


if __name__ == "__main__":
    unittest.main()
