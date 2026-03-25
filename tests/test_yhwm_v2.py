from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from yhwm import cli as cli_module
from yhwm.errors import WorkflowError
from yhwm.integration import INIT_BLOCK_END, INIT_BLOCK_START, install_hammerspoon, install_yabai_signals
from yhwm.models import (
    AltTabSession,
    EligibleWorkflowSpace,
    FocusGuard,
    ManagedSpaceState,
    ManagedTile,
    PendingSplit,
    RuntimeState,
)
from yhwm.state import RuntimeStateStore
from yhwm.workflow import WorkflowRuntime


class RuntimeStateStoreTests(unittest.TestCase):
    def test_default_path_uses_v3_filename(self) -> None:
        self.assertTrue(str(RuntimeStateStore.default_path()).endswith("yhwm-state-v3.json"))

    def test_round_trip_v3_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state = RuntimeState(
                spaces={
                    workflow_space.storage_key: managed_space(
                        workflow_space,
                        [
                            tile(1, 101, [102, 103]),
                            tile(2, 201, [202]),
                        ],
                        last_focused_tile_id=2,
                        next_tile_id=5,
                        pending_split=PendingSplit(tile_id=2, direction="east"),
                    )
                },
                alttab_session=AltTabSession(
                    origin_window_id=201,
                    origin_tile_id=2,
                    origin_workflow_space=workflow_space,
                ),
                focus_guard=FocusGuard(
                    workflow_space=workflow_space,
                    target_window_id=201,
                ),
            )

            store.write(state)
            loaded = store.read()

            self.assertEqual(loaded, state)


class WorkflowRuntimeV3Tests(unittest.TestCase):
    def test_reseed_tracks_single_tile_with_hidden_windows(self) -> None:
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
            self.assertEqual(
                tracked,
                managed_space(
                    EligibleWorkflowSpace(display=1, space=2),
                    [tile(1, 101, [102, 103])],
                    last_focused_tile_id=1,
                    next_tile_id=2,
                ),
            )

    def test_float_space_clears_managed_space_and_same_space_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            other_space = EligibleWorkflowSpace(display=1, space=3)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [tile(1, 101, [102])],
                            pending_split=PendingSplit(tile_id=1, direction="east"),
                        ),
                        other_space.storage_key: managed_space(
                            other_space,
                            [tile(1, 301, [302])],
                        ),
                    },
                    alttab_session=AltTabSession(
                        origin_window_id=101,
                        origin_tile_id=1,
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
            final_state = state_store.read()
            self.assertNotIn(workflow_space.storage_key, final_state.spaces)
            self.assertIn(other_space.storage_key, final_state.spaces)
            self.assertIsNone(final_state.alttab_session)
            self.assertIsNone(final_state.focus_guard)

    def test_delete_tile_merges_membership_into_recent_visible_survivor(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [
                                tile(1, 101, [102]),
                                tile(2, 201, [202]),
                            ],
                            last_focused_tile_id=1,
                            next_tile_id=3,
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, has_focus=True),
                    eligible_window(102),
                    eligible_window(201),
                    eligible_window(202),
                ],
                focused_window_id=101,
                recent_window_id=201,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).delete_tile()

            self.assertEqual(
                client.actions,
                [("stack", 201, 101), ("stack", 201, 102), ("focus", 201)],
            )
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.tiles[2].visible_window_id, 201)
            self.assertEqual(tracked.tiles[2].hidden_window_ids, [202, 101, 102])
            self.assertEqual(tracked.last_focused_tile_id, 2)
            self.assertEqual(sorted(tracked.tiles), [2])

    def test_delete_tile_noops_when_only_one_tile_remains(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            initial_state = RuntimeState(
                spaces={
                    workflow_space.storage_key: managed_space(
                        workflow_space,
                        [tile(1, 101, [102])],
                    )
                },
                alttab_session=None,
                focus_guard=None,
            )
            state_store.write(initial_state)
            client = FakeYabaiClient(
                windows=[eligible_window(101, has_focus=True), eligible_window(102)],
                focused_window_id=101,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).delete_tile()

            self.assertEqual(client.actions, [])
            self.assertEqual(state_store.read(), initial_state)

    def test_split_borrows_hidden_window_from_another_tile(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [
                                tile(1, 101),
                                tile(2, 201, [202]),
                            ],
                            last_focused_tile_id=1,
                            next_tile_id=3,
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, has_focus=True),
                    eligible_window(201),
                    eligible_window(202),
                ],
                focused_window_id=101,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).split("south")

            self.assertEqual(
                client.actions,
                [("arm_split", 101, "south"), ("warp", 202, 101)],
            )
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(sorted(tracked.tiles), [1, 2, 3])
            self.assertEqual(tracked.tiles[2].hidden_window_ids, [])
            self.assertEqual(tracked.tiles[3].visible_window_id, 202)
            self.assertIsNone(tracked.pending_split)

    def test_split_promotes_hidden_window_from_focused_tile(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [tile(1, 101, [102])],
                            last_focused_tile_id=1,
                            next_tile_id=2,
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(101, has_focus=True), eligible_window(102)],
                focused_window_id=101,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).split("south")

            self.assertEqual(
                client.actions,
                [("promote", 102, "south")],
            )
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(sorted(tracked.tiles), [1, 2])
            self.assertEqual(tracked.tiles[1].visible_window_id, 101)
            self.assertEqual(tracked.tiles[1].hidden_window_ids, [])
            self.assertEqual(tracked.tiles[2].visible_window_id, 102)
            self.assertEqual(tracked.tiles[2].hidden_window_ids, [])
            self.assertIsNone(tracked.pending_split)

    def test_native_focus_signal_after_split_updates_next_stack_first_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [tile(1, 101, [102])],
                            last_focused_tile_id=1,
                            next_tile_id=2,
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(101, has_focus=True), eligible_window(102)],
                focused_window_id=101,
                recent_window_id=101,
            )
            runtime = WorkflowRuntime(yabai=client, state_store=state_store)

            runtime.split("south")

            client._set_focus(102)
            runtime.handle_focus(102)

            client._windows[301] = eligible_window(301)
            client._set_focus(301)
            runtime.handle_window_event(event="window_created", window_id=301)

            self.assertEqual(
                client.actions,
                [("promote", 102, "south"), ("stack", 102, 301), ("focus", 301)],
            )
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(sorted(tracked.tiles), [1, 2])
            self.assertEqual(tracked.tiles[1].visible_window_id, 101)
            self.assertEqual(tracked.tiles[2].visible_window_id, 301)
            self.assertEqual(tracked.tiles[2].hidden_window_ids, [102])
            self.assertEqual(tracked.last_focused_tile_id, 2)

    def test_split_arms_pending_when_no_hidden_window_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [tile(1, 101)],
                            last_focused_tile_id=1,
                            next_tile_id=2,
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
            self.assertEqual(tracked.pending_split, PendingSplit(tile_id=1, direction="east"))

    def test_focus_visible_window_only_updates_last_focused_tile(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            initial_state = RuntimeState(
                spaces={
                    workflow_space.storage_key: managed_space(
                        workflow_space,
                        [tile(1, 101, [102]), tile(2, 201, [202])],
                        last_focused_tile_id=1,
                        next_tile_id=3,
                    )
                },
                alttab_session=None,
                focus_guard=None,
            )
            state_store.write(initial_state)
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101),
                    eligible_window(102),
                    eligible_window(201, has_focus=True),
                    eligible_window(202),
                ],
                focused_window_id=201,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_focus(201)

            self.assertEqual(client.actions, [])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.tiles[1], initial_state.spaces["1:2"].tiles[1])
            self.assertEqual(tracked.tiles[2], initial_state.spaces["1:2"].tiles[2])
            self.assertEqual(tracked.last_focused_tile_id, 2)

    def test_focus_hidden_window_promotes_it_within_same_tile(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [tile(1, 101, [102, 103])],
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101),
                    eligible_window(102, has_focus=True),
                    eligible_window(103),
                ],
                focused_window_id=102,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_focus(102)

            self.assertEqual(client.actions, [])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.tiles[1].visible_window_id, 102)
            self.assertEqual(tracked.tiles[1].hidden_window_ids, [101, 103])

    def test_created_window_without_pending_split_auto_stacks_into_last_focused_tile(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [tile(1, 101, [102]), tile(2, 201)],
                            last_focused_tile_id=2,
                            next_tile_id=3,
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101),
                    eligible_window(102),
                    eligible_window(201),
                    eligible_window(301, has_focus=True),
                ],
                focused_window_id=301,
                recent_window_id=201,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_window_event(
                event="window_created",
                window_id=301,
            )

            self.assertEqual(client.actions, [("stack", 201, 301), ("focus", 301)])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.tiles[2].visible_window_id, 301)
            self.assertEqual(tracked.tiles[2].hidden_window_ids, [201])

    def test_created_window_with_pending_split_creates_new_tile(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [tile(1, 101)],
                            last_focused_tile_id=1,
                            next_tile_id=2,
                            pending_split=PendingSplit(tile_id=1, direction="east"),
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(101), eligible_window(301, has_focus=True)],
                focused_window_id=301,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_window_event(
                event="window_created",
                window_id=301,
            )

            self.assertEqual(client.actions, [])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(sorted(tracked.tiles), [1, 2])
            self.assertEqual(tracked.tiles[2].visible_window_id, 301)
            self.assertIsNone(tracked.pending_split)

    def test_created_window_batch_without_pending_split_absorbs_all_unknown_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [tile(1, 101, [102])],
                            last_focused_tile_id=1,
                            next_tile_id=2,
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101),
                    eligible_window(102),
                    eligible_window(301),
                    eligible_window(302, has_focus=True),
                    eligible_window(303),
                ],
                focused_window_id=302,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_window_event(
                event="window_created",
                window_id=301,
            )

            self.assertEqual(
                client.actions,
                [
                    ("stack", 101, 301),
                    ("stack", 101, 302),
                    ("stack", 101, 303),
                    ("focus", 302),
                ],
            )
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.tiles[1].visible_window_id, 302)
            self.assertEqual(tracked.tiles[1].hidden_window_ids, [101, 102, 301, 303])

    def test_created_window_batch_with_pending_split_creates_one_new_tile(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [tile(1, 101)],
                            last_focused_tile_id=1,
                            next_tile_id=2,
                            pending_split=PendingSplit(tile_id=1, direction="east"),
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
                    eligible_window(302, has_focus=True),
                    eligible_window(303),
                ],
                focused_window_id=302,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_window_event(
                event="window_created",
                window_id=301,
            )

            self.assertEqual(
                client.actions,
                [
                    ("stack", 302, 301),
                    ("stack", 302, 303),
                ],
            )
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(sorted(tracked.tiles), [1, 2])
            self.assertEqual(tracked.tiles[2].visible_window_id, 302)
            self.assertEqual(tracked.tiles[2].hidden_window_ids, [301, 303])
            self.assertIsNone(tracked.pending_split)

    def test_moved_window_without_pending_split_auto_stacks_into_last_focused_tile(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [tile(1, 101), tile(2, 201), tile(3, 301)],
                            last_focused_tile_id=3,
                            next_tile_id=4,
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101),
                    eligible_window(201),
                    eligible_window(301),
                    eligible_window(401, has_focus=True),
                ],
                focused_window_id=401,
                recent_window_id=301,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_window_event(
                event="window_moved",
                window_id=401,
            )

            self.assertEqual(client.actions, [("stack", 301, 401), ("focus", 401)])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.tiles[3].visible_window_id, 401)
            self.assertEqual(tracked.tiles[3].hidden_window_ids, [301])

    def test_moved_window_with_pending_split_creates_new_tile(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [tile(1, 101)],
                            last_focused_tile_id=1,
                            next_tile_id=2,
                            pending_split=PendingSplit(tile_id=1, direction="east"),
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(101), eligible_window(301, has_focus=True)],
                focused_window_id=301,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_window_event(
                event="window_moved",
                window_id=301,
            )

            self.assertEqual(client.actions, [])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(sorted(tracked.tiles), [1, 2])
            self.assertEqual(tracked.tiles[2].visible_window_id, 301)
            self.assertIsNone(tracked.pending_split)

    def test_created_window_event_noops_when_window_was_already_absorbed_by_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            initial_state = RuntimeState(
                spaces={
                    workflow_space.storage_key: managed_space(
                        workflow_space,
                        [tile(1, 302, [101, 102, 301, 303])],
                        last_focused_tile_id=1,
                        next_tile_id=2,
                    )
                },
                alttab_session=None,
                focus_guard=None,
            )
            state_store.write(initial_state)
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101),
                    eligible_window(102),
                    eligible_window(301),
                    eligible_window(302, has_focus=True),
                    eligible_window(303),
                ],
                focused_window_id=302,
                recent_window_id=302,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_window_event(
                event="window_created",
                window_id=301,
            )

            self.assertEqual(client.actions, [])
            self.assertEqual(state_store.read(), initial_state)

    def test_focus_event_absorbs_multi_window_arrival_batch_instead_of_clearing_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [tile(1, 101, [102])],
                            last_focused_tile_id=1,
                            next_tile_id=2,
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101),
                    eligible_window(102),
                    eligible_window(301),
                    eligible_window(302, has_focus=True),
                    eligible_window(303),
                ],
                focused_window_id=302,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_focus(302)

            self.assertEqual(
                client.actions,
                [
                    ("stack", 101, 301),
                    ("stack", 101, 302),
                    ("stack", 101, 303),
                    ("focus", 302),
                ],
            )
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.tiles[1].visible_window_id, 302)
            self.assertEqual(tracked.tiles[1].hidden_window_ids, [101, 102, 301, 303])

    def test_window_moved_tracked_window_keeps_existing_non_arrival_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            initial_state = RuntimeState(
                spaces={
                    workflow_space.storage_key: managed_space(
                        workflow_space,
                        [tile(1, 101, [102])],
                    )
                },
                alttab_session=None,
                focus_guard=None,
            )
            state_store.write(initial_state)
            client = FakeYabaiClient(
                windows=[eligible_window(101, has_focus=True), eligible_window(102)],
                focused_window_id=101,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_window_event(
                event="window_moved",
                window_id=101,
            )

            self.assertEqual(client.actions, [])
            self.assertEqual(state_store.read(), initial_state)

    def test_window_moved_unknown_window_on_untracked_space_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            initial_state = RuntimeState(
                spaces={
                    workflow_space.storage_key: managed_space(
                        workflow_space,
                        [tile(1, 101, [102])],
                    )
                },
                alttab_session=None,
                focus_guard=None,
            )
            state_store.write(initial_state)
            client = FakeYabaiClient(
                windows=[eligible_window(101, has_focus=True), eligible_window(102), eligible_window(999, space=3)],
                focused_window_id=101,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_window_event(
                event="window_moved",
                window_id=999,
            )

            self.assertEqual(client.actions, [])
            self.assertEqual(state_store.read(), initial_state)

    def test_visible_window_loss_promotes_same_tile_hidden_window(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [tile(1, 101, [102, 103]), tile(2, 201)],
                            last_focused_tile_id=1,
                            next_tile_id=3,
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(102), eligible_window(103), eligible_window(201, has_focus=True)],
                focused_window_id=201,
                recent_window_id=201,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_window_event(
                event="window_destroyed",
                window_id=101,
            )

            self.assertEqual(client.actions, [])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.tiles[1].visible_window_id, 102)
            self.assertEqual(tracked.tiles[1].hidden_window_ids, [103])
            self.assertEqual(sorted(tracked.tiles), [1, 2])

    def test_visible_window_loss_without_hidden_removes_tile(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [tile(1, 101), tile(2, 201, [202])],
                            last_focused_tile_id=1,
                            next_tile_id=3,
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(201, has_focus=True), eligible_window(202)],
                focused_window_id=201,
                recent_window_id=201,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_window_event(
                event="window_destroyed",
                window_id=101,
            )

            self.assertEqual(client.actions, [])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(sorted(tracked.tiles), [2])
            self.assertEqual(tracked.last_focused_tile_id, 2)

    def test_hidden_window_loss_removes_it_from_tile(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [tile(1, 101, [102, 103])],
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(101, has_focus=True), eligible_window(103)],
                focused_window_id=101,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).handle_window_event(
                event="window_destroyed",
                window_id=102,
            )

            self.assertEqual(client.actions, [])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.tiles[1].hidden_window_ids, [103])

    def test_alttab_visible_swap_swaps_tile_occupants(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [tile(1, 101, [102]), tile(2, 201, [202])],
                            last_focused_tile_id=1,
                            next_tile_id=3,
                        )
                    },
                    alttab_session=AltTabSession(
                        origin_window_id=101,
                        origin_tile_id=1,
                        origin_workflow_space=workflow_space,
                    ),
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101),
                    eligible_window(102),
                    eligible_window(201, has_focus=True),
                    eligible_window(202),
                ],
                focused_window_id=201,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).alttab_release(201)

            self.assertEqual(client.actions, [("swap", 101, 201), ("focus", 201)])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.tiles[1].visible_window_id, 201)
            self.assertEqual(tracked.tiles[2].visible_window_id, 101)
            self.assertIsNone(state_store.read().alttab_session)

    def test_alttab_background_selection_moves_hidden_window_into_origin_tile(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [tile(1, 101, [102]), tile(2, 201, [202])],
                            last_focused_tile_id=1,
                            next_tile_id=3,
                        )
                    },
                    alttab_session=AltTabSession(
                        origin_window_id=101,
                        origin_tile_id=1,
                        origin_workflow_space=workflow_space,
                    ),
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(101), eligible_window(102, has_focus=True), eligible_window(201), eligible_window(202)],
                focused_window_id=102,
                recent_window_id=101,
            )

            WorkflowRuntime(yabai=client, state_store=state_store).alttab_release(202)

            self.assertEqual(client.actions, [("stack", 101, 202), ("focus", 202)])
            tracked = state_store.read().spaces["1:2"]
            self.assertEqual(tracked.tiles[1].visible_window_id, 202)
            self.assertEqual(tracked.tiles[1].hidden_window_ids, [101, 102])
            self.assertEqual(tracked.tiles[2].hidden_window_ids, [])

    def test_alttab_cancel_sets_focus_guard_and_next_focus_clears_it(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [tile(1, 101, [102])],
                        )
                    },
                    alttab_session=AltTabSession(
                        origin_window_id=101,
                        origin_tile_id=1,
                        origin_workflow_space=workflow_space,
                    ),
                    focus_guard=None,
                )
            )
            runtime = WorkflowRuntime(
                yabai=FakeYabaiClient(
                    windows=[eligible_window(101, has_focus=True), eligible_window(102)],
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
            self.assertEqual(final_state.spaces["1:2"].tiles[1].visible_window_id, 101)

    def test_split_clears_space_and_raises_on_unknown_live_window_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            state_store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: managed_space(
                            workflow_space,
                            [tile(1, 101)],
                        )
                    },
                    alttab_session=None,
                    focus_guard=None,
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(101, has_focus=True), eligible_window(202)],
                focused_window_id=101,
                recent_window_id=101,
            )

            with self.assertRaises(WorkflowError):
                WorkflowRuntime(yabai=client, state_store=state_store).split("east")

            self.assertEqual(state_store.read().spaces, {})


class CliTests(unittest.TestCase):
    def test_delete_tile_command_dispatches_runtime_delete_tile(self) -> None:
        runtime = MagicMock()
        with patch.object(cli_module, "SubprocessYabaiClient", return_value=object()):
            with patch.object(cli_module, "WorkflowRuntime", return_value=runtime):
                result = cli_module.main(["delete-tile"])

        self.assertEqual(result, 0)
        runtime.delete_tile.assert_called_once_with()

    def test_float_command_dispatches_runtime_float_space(self) -> None:
        runtime = MagicMock()
        with patch.object(cli_module, "SubprocessYabaiClient", return_value=object()):
            with patch.object(cli_module, "WorkflowRuntime", return_value=runtime):
                result = cli_module.main(["float"])

        self.assertEqual(result, 0)
        runtime.float_space.assert_called_once_with()


class IntegrationInstallTests(unittest.TestCase):
    def test_install_yabai_signals_registers_runtime_surface(self) -> None:
        client = FakeYabaiClient(
            windows=[eligible_window(101, has_focus=True)],
            focused_window_id=101,
            recent_window_id=101,
        )

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

    def warp_window(self, window_id: int, target_window_id: int) -> None:
        self.actions.append(("warp", window_id, target_window_id))

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


def tile(tile_id: int, visible_window_id: int, hidden_window_ids: list[int] | None = None) -> ManagedTile:
    return ManagedTile(
        tile_id=tile_id,
        visible_window_id=visible_window_id,
        hidden_window_ids=list(hidden_window_ids or []),
    )


def managed_space(
    workflow_space: EligibleWorkflowSpace,
    tiles: list[ManagedTile],
    *,
    last_focused_tile_id: int | None = None,
    next_tile_id: int | None = None,
    pending_split: PendingSplit | None = None,
) -> ManagedSpaceState:
    tile_map = {entry.tile_id: entry for entry in tiles}
    return ManagedSpaceState(
        workflow_space=workflow_space,
        tiles=tile_map,
        last_focused_tile_id=last_focused_tile_id or min(tile_map),
        next_tile_id=next_tile_id or (max(tile_map) + 1),
        pending_split=pending_split,
    )


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
        "is-visible": True,
        "has-focus": has_focus,
    }
    window.update(overrides)
    return window
