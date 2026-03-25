from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from yhwm import cli as cli_module
from yhwm.integration import INIT_BLOCK_END, INIT_BLOCK_START, install_hammerspoon, install_yabai_signals
from yhwm.models import AltTabSession, EligibleWorkflowSpace, FocusGuard, RuntimeState, TrackedSpaceState
from yhwm.state import RuntimeStateStore
from yhwm.workflow import WorkflowRuntime


class WorkflowRuntimeV2Tests(unittest.TestCase):
    def test_reseed_tracks_focused_window_as_leader(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, has_focus=True),
                    eligible_window(102),
                    eligible_window(103),
                    eligible_window(999, **{"is-floating": True}),
                ],
                focused_window_id=101,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).reseed()

            self.assertEqual(
                client.actions,
                [
                    ("set_layout", 2, "bsp"),
                    ("stack", 101, 102),
                    ("stack", 101, 103),
                    ("focus", 101),
                ],
            )
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.leader_window_id, 101)
            self.assertEqual(tracked.background_window_ids, [102, 103])
            self.assertIsNone(tracked.pending_split_direction)

    def test_float_space_converts_tracked_space_and_removes_it_from_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            other_space = EligibleWorkflowSpace(display=1, space=3)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: TrackedSpaceState(
                            workflow_space=workflow_space,
                            leader_window_id=101,
                            background_window_ids=[102],
                            pending_split_direction="east",
                        ),
                        other_space.storage_key: TrackedSpaceState(
                            workflow_space=other_space,
                            leader_window_id=301,
                            background_window_ids=[302],
                            pending_split_direction=None,
                        ),
                    },
                    alttab_session=AltTabSession(
                        origin_window_id=101,
                        origin_workflow_space=workflow_space,
                    ),
                    focus_guard=FocusGuard(
                        workflow_space=workflow_space,
                        target_window_id=101,
                    ),
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(101, has_focus=True), eligible_window(102)],
                focused_window_id=101,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).float_space()

            self.assertEqual(client.actions, [("set_layout", 2, "float")])
            state = state_store.read()
            self.assertNotIn(workflow_space.storage_key, state.spaces)
            self.assertIn(other_space.storage_key, state.spaces)
            self.assertIsNone(state.alttab_session)
            self.assertIsNone(state.focus_guard)

    def test_float_space_noops_when_current_space_is_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            other_space = EligibleWorkflowSpace(display=1, space=3)
            initial_state = RuntimeState(
                spaces={
                    other_space.storage_key: TrackedSpaceState(
                        workflow_space=other_space,
                        leader_window_id=301,
                        background_window_ids=[],
                        pending_split_direction=None,
                    )
                },
                alttab_session=None,
                focus_guard=None,
            )
            state_store.write(initial_state)
            client = FakeYabaiClient(
                windows=[eligible_window(101, has_focus=True)],
                focused_window_id=101,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).float_space()

            self.assertEqual(client.actions, [])
            self.assertEqual(state_store.read(), initial_state)

    def test_float_space_noops_when_focused_window_is_ineligible(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            initial_state = RuntimeState(
                spaces={
                    workflow_space.storage_key: TrackedSpaceState(
                        workflow_space=workflow_space,
                        leader_window_id=101,
                        background_window_ids=[102],
                        pending_split_direction=None,
                    )
                },
                alttab_session=None,
                focus_guard=None,
            )
            state_store.write(initial_state)
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, has_focus=True, **{"is-floating": True}),
                    eligible_window(102),
                ],
                focused_window_id=101,
                recent_window_id=101,
                layout_by_space={2: "float"},
            )

            WorkflowRuntime(yabai=client, state_store=state_store).float_space()

            self.assertEqual(client.actions, [])
            self.assertEqual(state_store.read(), initial_state)

    def test_float_space_preserves_other_space_session_and_focus_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            other_space = EligibleWorkflowSpace(display=1, space=3)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: TrackedSpaceState(
                            workflow_space=workflow_space,
                            leader_window_id=101,
                            background_window_ids=[],
                            pending_split_direction=None,
                        ),
                        other_space.storage_key: TrackedSpaceState(
                            workflow_space=other_space,
                            leader_window_id=301,
                            background_window_ids=[],
                            pending_split_direction=None,
                        ),
                    },
                    alttab_session=AltTabSession(
                        origin_window_id=301,
                        origin_workflow_space=other_space,
                    ),
                    focus_guard=FocusGuard(
                        workflow_space=other_space,
                        target_window_id=301,
                    ),
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(101, has_focus=True)],
                focused_window_id=101,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).float_space()

            state = state_store.read()
            self.assertEqual(client.actions, [("set_layout", 2, "float")])
            self.assertNotIn(workflow_space.storage_key, state.spaces)
            self.assertIsNotNone(state.alttab_session)
            self.assertEqual(state.alttab_session.origin_workflow_space, other_space)
            self.assertEqual(
                state.focus_guard,
                FocusGuard(workflow_space=other_space, target_window_id=301),
            )

    def test_split_promotes_one_background_window(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: TrackedSpaceState(
                            workflow_space=workflow_space,
                            leader_window_id=101,
                            background_window_ids=[102, 103],
                            pending_split_direction=None,
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(101, has_focus=True), eligible_window(102), eligible_window(103)],
                focused_window_id=101,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).split("south")

            self.assertEqual(client.actions, [("promote", 102, "south"), ("focus", 101)])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.background_window_ids, [103])
            self.assertIsNone(tracked.pending_split_direction)

    def test_split_arms_pending_when_background_pool_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: TrackedSpaceState(
                            workflow_space=workflow_space,
                            leader_window_id=101,
                            background_window_ids=[],
                            pending_split_direction=None,
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(101, has_focus=True)],
                focused_window_id=101,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).split("east")

            self.assertEqual(client.actions, [("arm_split", 101, "east")])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.pending_split_direction, "east")

    def test_focus_visible_window_retargets_background_pool(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: TrackedSpaceState(
                            workflow_space=workflow_space,
                            leader_window_id=101,
                            background_window_ids=[301, 302],
                            pending_split_direction=None,
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101),
                    eligible_window(202, has_focus=True),
                    eligible_window(301),
                    eligible_window(302),
                ],
                focused_window_id=202,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_focus(202)

            self.assertEqual(client.actions, [("stack", 202, 301), ("stack", 202, 302)])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.leader_window_id, 202)
            self.assertEqual(tracked.background_window_ids, [301, 302])

    def test_focus_background_window_swaps_leader_and_background_membership(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: TrackedSpaceState(
                            workflow_space=workflow_space,
                            leader_window_id=101,
                            background_window_ids=[301, 302],
                            pending_split_direction=None,
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101),
                    eligible_window(301, has_focus=True),
                    eligible_window(302),
                ],
                focused_window_id=301,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_focus(301)

            self.assertEqual(client.actions, [])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.leader_window_id, 301)
            self.assertEqual(tracked.background_window_ids, [302, 101])

    def test_created_window_without_pending_split_stacks_onto_leader_tile(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: TrackedSpaceState(
                            workflow_space=workflow_space,
                            leader_window_id=101,
                            background_window_ids=[301],
                            pending_split_direction=None,
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101),
                    eligible_window(301),
                    eligible_window(401, has_focus=True),
                ],
                focused_window_id=401,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_window_event(
                event="window_created",
                window_id=401,
            )

            self.assertEqual(
                client.actions,
                [("stack", 101, 401), ("focus", 401), ("stack", 401, 301)],
            )
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.leader_window_id, 401)
            self.assertEqual(tracked.background_window_ids, [301, 101])

    def test_created_window_with_pending_split_only_clears_pending_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: TrackedSpaceState(
                            workflow_space=workflow_space,
                            leader_window_id=101,
                            background_window_ids=[301],
                            pending_split_direction="east",
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101),
                    eligible_window(301),
                    eligible_window(401, has_focus=True),
                ],
                focused_window_id=401,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_window_event(
                event="window_created",
                window_id=401,
            )

            self.assertEqual(client.actions, [])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.leader_window_id, 101)
            self.assertEqual(tracked.background_window_ids, [301])
            self.assertIsNone(tracked.pending_split_direction)

    def test_created_window_after_focus_race_reanchors_background_pool(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: TrackedSpaceState(
                            workflow_space=workflow_space,
                            leader_window_id=401,
                            background_window_ids=[301],
                            pending_split_direction=None,
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101),
                    eligible_window(301),
                    eligible_window(401, has_focus=True),
                ],
                focused_window_id=401,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_window_event(
                event="window_created",
                window_id=401,
            )

            self.assertEqual(
                client.actions,
                [("stack", 101, 401), ("focus", 401), ("stack", 401, 301)],
            )
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.leader_window_id, 401)
            self.assertEqual(tracked.background_window_ids, [301, 101])

    def test_window_moved_ignores_tracked_window_that_is_still_eligible_in_same_space(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: TrackedSpaceState(
                            workflow_space=workflow_space,
                            leader_window_id=101,
                            background_window_ids=[301],
                            pending_split_direction=None,
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(101, has_focus=True), eligible_window(301)],
                focused_window_id=101,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_window_event(
                event="window_moved",
                window_id=301,
            )

            self.assertEqual(client.actions, [])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.background_window_ids, [301])

    def test_leader_loss_promotes_visible_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: TrackedSpaceState(
                            workflow_space=workflow_space,
                            leader_window_id=101,
                            background_window_ids=[301],
                            pending_split_direction="south",
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(202, has_focus=True), eligible_window(301)],
                focused_window_id=202,
                recent_window_id=202,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_window_event(
                event="window_destroyed",
                window_id=101,
            )

            self.assertEqual(client.actions, [("stack", 202, 301)])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.leader_window_id, 202)
            self.assertEqual(tracked.background_window_ids, [301])
            self.assertIsNone(tracked.pending_split_direction)

    def test_leader_loss_can_promote_background_when_no_visible_window_remains(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: TrackedSpaceState(
                            workflow_space=workflow_space,
                            leader_window_id=101,
                            background_window_ids=[301, 302],
                            pending_split_direction=None,
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(301), eligible_window(302)],
                focused_window_id=301,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_window_event(
                event="window_destroyed",
                window_id=101,
            )

            self.assertEqual(client.actions, [("focus", 301)])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.leader_window_id, 301)
            self.assertEqual(tracked.background_window_ids, [302])

    def test_alttab_visible_swap_retargets_background_pool(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: TrackedSpaceState(
                            workflow_space=workflow_space,
                            leader_window_id=101,
                            background_window_ids=[301],
                            pending_split_direction=None,
                        )
                    },
                    alttab_session=AltTabSession(
                        origin_window_id=101,
                        origin_workflow_space=workflow_space,
                    ),
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(101), eligible_window(202, has_focus=True), eligible_window(301)],
                focused_window_id=202,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).alttab_release(202)

            self.assertEqual(client.actions, [("swap", 101, 202), ("focus", 202), ("stack", 202, 301)])
            state = state_store.read()
            self.assertIsNone(state.alttab_session)
            tracked = state.spaces["1:2"]
            self.assertEqual(tracked.leader_window_id, 202)
            self.assertEqual(tracked.background_window_ids, [301])

    def test_alttab_background_selection_focuses_selected_window_and_rewrites_membership(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: TrackedSpaceState(
                            workflow_space=workflow_space,
                            leader_window_id=101,
                            background_window_ids=[301, 302],
                            pending_split_direction=None,
                        )
                    },
                    alttab_session=AltTabSession(
                        origin_window_id=101,
                        origin_workflow_space=workflow_space,
                    ),
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(101), eligible_window(301, has_focus=True), eligible_window(302)],
                focused_window_id=301,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).alttab_release(301)

            self.assertEqual(client.actions, [("focus", 301)])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.leader_window_id, 301)
            self.assertEqual(tracked.background_window_ids, [302, 101])

    def test_alttab_cancel_sets_focus_guard_and_next_focus_consumes_it(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: TrackedSpaceState(
                            workflow_space=workflow_space,
                            leader_window_id=101,
                            background_window_ids=[301],
                            pending_split_direction=None,
                        )
                    },
                    alttab_session=AltTabSession(
                        origin_window_id=101,
                        origin_workflow_space=workflow_space,
                    ),
                    focus_guard=None,
                )
            )
            runtime = WorkflowRuntime(
                yabai=FakeYabaiClient(
                    windows=[eligible_window(101, has_focus=True), eligible_window(301)],
                    focused_window_id=101,
                    recent_window_id=101,
                ),
                state_store=state_store,
            )

            runtime.alttab_cancel(reason="chooser_close", window_id=101)
            canceled_state = state_store.read()
            self.assertIsNone(canceled_state.alttab_session)
            self.assertEqual(
                canceled_state.focus_guard,
                FocusGuard(workflow_space=workflow_space, target_window_id=101),
            )

            runtime.handle_focus(101)

            final_state = state_store.read()
            self.assertIsNone(final_state.focus_guard)
            self.assertEqual(final_state.spaces["1:2"].leader_window_id, 101)


class CliTests(unittest.TestCase):
    def test_float_command_dispatches_runtime_float_space(self) -> None:
        runtime = MagicMock()
        with patch.object(cli_module, "SubprocessYabaiClient", return_value=object()):
            with patch.object(cli_module, "WorkflowRuntime", return_value=runtime):
                result = cli_module.main(["float"])

        self.assertEqual(result, 0)
        runtime.float_space.assert_called_once_with()


class IntegrationInstallTests(unittest.TestCase):
    def test_install_yabai_signals_registers_v2_surface(self) -> None:
        client = FakeYabaiClient(windows=[eligible_window(101, has_focus=True)], focused_window_id=101, recent_window_id=101)

        install_yabai_signals(
            yabai=client,
            executable_path="/tmp/runtime/bin/yhwm",
        )

        self.assertEqual(
            client.signal_actions,
            [
                ("remove_signal", "yhwm_v2_window_focused"),
                ("remove_signal", "yhwm_v2_window_created"),
                ("remove_signal", "yhwm_v2_window_deminimized"),
                ("remove_signal", "yhwm_v2_window_moved"),
                ("remove_signal", "yhwm_v2_window_minimized"),
                ("remove_signal", "yhwm_v2_window_destroyed"),
                ("add_signal", "window_focused", '/tmp/runtime/bin/yhwm signal focus --window-id "$YABAI_WINDOW_ID"', "yhwm_v2_window_focused"),
                ("add_signal", "window_created", '/tmp/runtime/bin/yhwm signal window --event window_created --window-id "$YABAI_WINDOW_ID"', "yhwm_v2_window_created"),
                ("add_signal", "window_deminimized", '/tmp/runtime/bin/yhwm signal window --event window_deminimized --window-id "$YABAI_WINDOW_ID"', "yhwm_v2_window_deminimized"),
                ("add_signal", "window_moved", '/tmp/runtime/bin/yhwm signal window --event window_moved --window-id "$YABAI_WINDOW_ID"', "yhwm_v2_window_moved"),
                ("add_signal", "window_minimized", '/tmp/runtime/bin/yhwm signal window --event window_minimized --window-id "$YABAI_WINDOW_ID"', "yhwm_v2_window_minimized"),
                ("add_signal", "window_destroyed", '/tmp/runtime/bin/yhwm signal window --event window_destroyed --window-id "$YABAI_WINDOW_ID"', "yhwm_v2_window_destroyed"),
            ],
        )

    def test_install_hammerspoon_replaces_managed_block_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            runtime_root = Path(tempdir) / "runtime"
            module_path = runtime_root / "hammerspoon" / "yhwm.lua"
            module_path.parent.mkdir(parents=True)
            module_path.write_text("return {}\n", encoding="utf-8")
            fake_home = Path(tempdir) / "home"
            hammerspoon_home = fake_home / ".hammerspoon"
            hammerspoon_home.mkdir(parents=True)
            init_path = hammerspoon_home / "init.lua"
            init_path.write_text('require("hs.ipc")\n', encoding="utf-8")

            with patch("pathlib.Path.home", return_value=fake_home):
                with patch("subprocess.run", return_value=CompletedProcessStub(0)):
                    install_hammerspoon(
                        runtime_root=runtime_root,
                        executable_path="/tmp/runtime/bin/yhwm",
                        hs_bin="/opt/homebrew/bin/hs",
                    )
                    install_hammerspoon(
                        runtime_root=runtime_root,
                        executable_path="/tmp/runtime/bin/yhwm",
                        hs_bin="/opt/homebrew/bin/hs",
                    )

            init_text = init_path.read_text(encoding="utf-8")
            self.assertEqual(init_text.count(INIT_BLOCK_START), 1)
            self.assertEqual(init_text.count(INIT_BLOCK_END), 1)
            self.assertIn('/tmp/runtime/bin/yhwm', init_text)


class CompletedProcessStub:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeYabaiClient:
    def __init__(
        self,
        *,
        windows: list[dict],
        focused_window_id: int,
        recent_window_id: int,
        focus_follows_mouse: str = "off",
        mouse_follows_focus: str = "off",
        layout_by_space: dict[int, str] | None = None,
    ):
        self._windows = {int(window["id"]): dict(window) for window in windows}
        self._focused_window_id = focused_window_id
        self._recent_window_id = recent_window_id
        self._focus_follows_mouse = focus_follows_mouse
        self._mouse_follows_focus = mouse_follows_focus
        self._layout_by_space = layout_by_space or {2: "bsp"}
        self.actions: list[tuple] = []
        self.signal_actions: list[tuple] = []

    def get_config(self, setting: str, *, space: int | None = None) -> str:
        if setting == "focus_follows_mouse":
            return self._focus_follows_mouse
        if setting == "mouse_follows_focus":
            return self._mouse_follows_focus
        if setting == "layout":
            return self._layout_by_space.get(space or 2, "bsp")
        raise AssertionError(f"Unexpected config lookup: {setting}")

    def query_focused_window(self):
        return self.query_window(self._focused_window_id)

    def query_window(self, window_id: int):
        if window_id not in self._windows:
            from yhwm.errors import WorkflowError

            raise WorkflowError(f"missing window {window_id}")
        return dict(self._windows[window_id])

    def query_recent_window(self):
        return self.query_window(self._recent_window_id)

    def query_display(self, display: int):
        return {"index": display}

    def query_space(self, space: int):
        return {
            "index": space,
            "display": 1,
            "is-visible": True,
            "is-native-fullscreen": False,
        }

    def query_windows_for_space(self, space: int):
        return [dict(window) for window in self._windows.values() if window["space"] == space]

    def set_space_layout(self, space: int, layout: str) -> None:
        self.actions.append(("set_layout", space, layout))
        self._layout_by_space[space] = layout

    def stack_window(self, anchor_window_id: int, candidate_window_id: int) -> None:
        self.actions.append(("stack", anchor_window_id, candidate_window_id))

    def promote_stacked_window(self, window_id: int, direction: str) -> None:
        self.actions.append(("promote", window_id, direction))

    def arm_window_split(self, window_id: int, direction: str) -> None:
        self.actions.append(("arm_split", window_id, direction))

    def focus_window(self, window_id: int) -> None:
        self.actions.append(("focus", window_id))
        if window_id in self._windows:
            self._set_focus(window_id)

    def swap_window(self, window_id: int, target_window_id: int) -> None:
        self.actions.append(("swap", window_id, target_window_id))

    def add_signal(self, event: str, action: str, label: str) -> None:
        self.signal_actions.append(("add_signal", event, action, label))

    def remove_signal(self, signal_selector: str) -> None:
        self.signal_actions.append(("remove_signal", signal_selector))

    def _set_focus(self, window_id: int) -> None:
        for candidate_window_id, window in self._windows.items():
            window["has-focus"] = candidate_window_id == window_id
        self._recent_window_id = self._focused_window_id
        self._focused_window_id = window_id


def eligible_window(
    window_id: int,
    *,
    display: int = 1,
    space: int = 2,
    has_focus: bool = False,
    **overrides,
) -> dict:
    window = {
        "id": window_id,
        "display": display,
        "space": space,
        "root-window": True,
        "role": "AXWindow",
        "subrole": "AXStandardWindow",
        "can-move": True,
        "has-ax-reference": True,
        "layer": "normal",
        "level": 0,
        "is-floating": False,
        "is-sticky": False,
        "is-native-fullscreen": False,
        "is-minimized": False,
        "is-hidden": False,
        "has-focus": has_focus,
    }
    window.update(overrides)
    return window
