from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from windy import cli as cli_module
from windy.errors import WorkflowError
from windy.hammerspoon import SubprocessHammerspoonClient
from windy.integration import INIT_BLOCK_END, INIT_BLOCK_START, install_hammerspoon
from windy.models import EligibleWorkflowSpace, NormalizedFrame, PendingSplit, RuntimeState, TrackedSpaceState
from windy.state import RuntimeStateStore
from windy.workflow import WorkflowRuntime
from windy.yabai import SubprocessYabaiClient


class RuntimeStateStoreTests(unittest.TestCase):
    def test_read_ignores_old_schema_and_returns_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "state.json"
            path.write_text('{"schema_version": 4, "spaces": {}}\n', encoding="utf-8")

            state = RuntimeStateStore(path).read()

            self.assertEqual(state, RuntimeState.empty())

    def test_default_path_uses_windy_state_name(self) -> None:
        self.assertEqual(RuntimeStateStore.default_path().name, "windy-state.json")

    def test_write_and_read_round_trip_pending_split(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "state.json"
            store = RuntimeStateStore(path)
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            original = RuntimeState(
                spaces={
                    workflow_space.storage_key: TrackedSpaceState(
                        workflow_space=workflow_space,
                        pending_split=PendingSplit(
                            direction="east",
                            anchor_window_id=101,
                            anchor_frame=NormalizedFrame(x=0, y=0, w=100, h=50),
                        ),
                    )
                }
            )

            store.write(original)

            self.assertEqual(store.read(), original)


class WorkflowRuntimeTests(unittest.TestCase):
    def test_reseed_tracks_space_and_stacks_other_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, frame=frame(0, 0, 100, 100), has_focus=True),
                    eligible_window(102, frame=frame(0, 0, 100, 100)),
                    eligible_window(103, frame=frame(0, 0, 100, 100)),
                ],
                focused_window_id=101,
                recent_window_id=101,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([101, 102, 103]),
                state_store=store,
            )

            runtime.reseed()

            self.assertEqual(
                client.actions,
                [
                    ("set_layout", 2, "bsp"),
                    ("stack", 101, 102),
                    ("stack", 101, 103),
                    ("focus", 101),
                ],
            )
            self.assertEqual(
                store.read(),
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: TrackedSpaceState(
                            workflow_space=workflow_space,
                            pending_split=None,
                        )
                    }
                ),
            )

    def test_split_with_background_in_focused_tile_promotes_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: tracked_space(workflow_space),
                    }
                )
            )
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, frame=frame(0, 0, 100, 100), has_focus=True),
                    eligible_window(102, frame=frame(0, 0, 100, 100)),
                ],
                focused_window_id=101,
                recent_window_id=101,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([101, 102]),
                state_store=store,
            )

            runtime.split("east")

            self.assertEqual(client.actions, [("promote", 102, "east"), ("focus", 101)])
            self.assertIsNone(store.read().spaces["1:2"].pending_split)

    def test_split_with_no_background_arms_pending_split(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: tracked_space(workflow_space),
                    }
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(101, frame=frame(0, 0, 100, 100), has_focus=True)],
                focused_window_id=101,
                recent_window_id=101,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([101]),
                state_store=store,
            )

            runtime.split("south")

            self.assertEqual(client.actions, [("arm_split", 101, "south")])
            self.assertEqual(
                store.read().spaces["1:2"].pending_split,
                PendingSplit(
                    direction="south",
                    anchor_window_id=101,
                    anchor_frame=NormalizedFrame(x=0, y=0, w=100, h=100),
                ),
            )

    def test_navigate_clears_consumed_pending_split_before_focusing_direction(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: tracked_space(
                            workflow_space,
                            pending_split=PendingSplit(
                                direction="east",
                                anchor_window_id=101,
                                anchor_frame=NormalizedFrame(x=0, y=0, w=100, h=100),
                            ),
                        )
                    }
                )
            )
            client = FakeYabaiClient(
                windows=[eligible_window(101, frame=frame(0, 0, 50, 100), has_focus=True)],
                focused_window_id=101,
                recent_window_id=101,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([101]),
                state_store=store,
            )

            runtime.navigate("east")

            self.assertEqual(client.actions, [("focus_direction", "east")])
            self.assertIsNone(store.read().spaces["1:2"].pending_split)

    def test_delete_tile_merges_focused_tile_into_recent_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            store.write(RuntimeState(spaces={workflow_space.storage_key: tracked_space(workflow_space)}))
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, frame=frame(0, 0, 50, 100), has_focus=True),
                    eligible_window(102, frame=frame(0, 0, 50, 100)),
                    eligible_window(201, frame=frame(50, 0, 50, 100)),
                ],
                focused_window_id=101,
                recent_window_id=201,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([101, 201, 102]),
                state_store=store,
            )

            runtime.delete_tile()

            self.assertEqual(
                client.actions,
                [
                    ("stack", 201, 101),
                    ("stack", 201, 102),
                    ("focus", 201),
                ],
            )

    def test_float_clears_tracking(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            store.write(RuntimeState(spaces={workflow_space.storage_key: tracked_space(workflow_space)}))
            client = FakeYabaiClient(
                windows=[eligible_window(101, frame=frame(0, 0, 100, 100), has_focus=True)],
                focused_window_id=101,
                recent_window_id=101,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([101]),
                state_store=store,
            )

            runtime.float_space()

            self.assertEqual(client.actions, [("set_layout", 2, "float")])
            self.assertEqual(store.read().spaces, {})

    def test_alttab_visible_target_swaps_visible_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            store.write(RuntimeState(spaces={workflow_space.storage_key: tracked_space(workflow_space)}))
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, frame=frame(0, 0, 50, 100)),
                    eligible_window(201, frame=frame(50, 0, 50, 100), has_focus=True),
                ],
                focused_window_id=201,
                recent_window_id=101,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([201, 101]),
                state_store=store,
            )

            runtime.alttab(
                origin_window_id=101,
                selected_window_id=201,
                origin_open_frame=NormalizedFrame(x=0, y=0, w=50, h=100),
                selected_open_frame=NormalizedFrame(x=50, y=0, w=50, h=100),
                selected_was_visible_at_open=True,
            )

            self.assertEqual(client.actions, [("swap", 101, 201), ("focus", 201)])

    def test_alttab_background_target_replaces_origin_tile(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            store.write(RuntimeState(spaces={workflow_space.storage_key: tracked_space(workflow_space)}))
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, frame=frame(0, 0, 50, 100)),
                    eligible_window(201, frame=frame(50, 0, 50, 100)),
                    eligible_window(202, frame=frame(50, 0, 50, 100), has_focus=True),
                ],
                focused_window_id=202,
                recent_window_id=101,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([202, 101, 201]),
                state_store=store,
            )

            runtime.alttab(
                origin_window_id=101,
                selected_window_id=202,
                origin_open_frame=NormalizedFrame(x=0, y=0, w=50, h=100),
                selected_open_frame=NormalizedFrame(x=50, y=0, w=50, h=100),
                selected_was_visible_at_open=False,
            )

            self.assertEqual(client.actions, [("stack", 101, 202), ("focus", 202)])

    def test_alttab_same_tile_selection_only_refocuses(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            store.write(RuntimeState(spaces={workflow_space.storage_key: tracked_space(workflow_space)}))
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, frame=frame(0, 0, 100, 100)),
                    eligible_window(102, frame=frame(0, 0, 100, 100), has_focus=True),
                ],
                focused_window_id=102,
                recent_window_id=101,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([102, 101]),
                state_store=store,
            )

            runtime.alttab(
                origin_window_id=101,
                selected_window_id=102,
                origin_open_frame=NormalizedFrame(x=0, y=0, w=100, h=100),
                selected_open_frame=NormalizedFrame(x=0, y=0, w=100, h=100),
                selected_was_visible_at_open=False,
            )

            self.assertEqual(client.actions, [("focus", 102)])

    def test_alttab_cross_space_target_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            store.write(RuntimeState(spaces={workflow_space.storage_key: tracked_space(workflow_space)}))
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, frame=frame(0, 0, 100, 100), space=2),
                    eligible_window(301, frame=frame(0, 0, 100, 100), space=3, has_focus=True),
                ],
                focused_window_id=301,
                recent_window_id=101,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([301, 101]),
                state_store=store,
            )

            runtime.alttab(
                origin_window_id=101,
                selected_window_id=301,
                origin_open_frame=NormalizedFrame(x=0, y=0, w=100, h=100),
                selected_open_frame=NormalizedFrame(x=0, y=0, w=100, h=100),
                selected_was_visible_at_open=True,
            )

            self.assertEqual(client.actions, [])

    def test_on_window_created_exits_for_untracked_space(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, frame=frame(0, 0, 100, 100), has_focus=True),
                ],
                focused_window_id=101,
                recent_window_id=101,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([101]),
                state_store=store,
            )

            runtime.on_window_created(101)

            self.assertEqual(client.actions, [("rediscover", 101)])

    def test_on_window_created_absorbs_solo_tile(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            store.write(RuntimeState(spaces={workflow_space.storage_key: tracked_space(workflow_space)}))
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, frame=frame(0, 0, 50, 100)),
                    eligible_window(102, frame=frame(0, 0, 50, 100)),
                    eligible_window(201, frame=frame(50, 0, 50, 100), has_focus=True),
                ],
                focused_window_id=201,
                recent_window_id=101,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([201, 101, 102]),
                state_store=store,
            )

            runtime.on_window_created(201)

            self.assertIn(("stack", 101, 201), client.actions)
            self.assertIn(("focus", 201), client.actions)

    def test_on_window_created_absorbs_only_triggering_window(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            store.write(RuntimeState(spaces={workflow_space.storage_key: tracked_space(workflow_space)}))
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, frame=frame(0, 0, 34, 100)),
                    eligible_window(102, frame=frame(0, 0, 34, 100)),
                    eligible_window(201, frame=frame(34, 0, 33, 100)),
                    eligible_window(301, frame=frame(67, 0, 33, 100), has_focus=True),
                ],
                focused_window_id=301,
                recent_window_id=101,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([301, 101, 102, 201]),
                state_store=store,
            )

            runtime.on_window_created(301)

            stack_actions = [a for a in client.actions if a[0] == "stack"]
            self.assertEqual(len(stack_actions), 1)
            self.assertEqual(stack_actions[0], ("stack", 101, 301))

    def test_on_window_created_idempotent_when_no_solo_tiles(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            store.write(RuntimeState(spaces={workflow_space.storage_key: tracked_space(workflow_space)}))
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, frame=frame(0, 0, 100, 100), has_focus=True),
                    eligible_window(102, frame=frame(0, 0, 100, 100)),
                ],
                focused_window_id=101,
                recent_window_id=101,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([101, 102]),
                state_store=store,
            )

            runtime.on_window_created(102)

            stack_actions = [a for a in client.actions if a[0] == "stack"]
            self.assertEqual(stack_actions, [])

    def test_on_window_created_preserves_intentional_split(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            store.write(
                RuntimeState(
                    spaces={
                        workflow_space.storage_key: tracked_space(
                            workflow_space,
                            pending_split=PendingSplit(
                                direction="east",
                                anchor_window_id=101,
                                anchor_frame=NormalizedFrame(x=0, y=0, w=100, h=100),
                            ),
                        ),
                    }
                )
            )
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, frame=frame(0, 0, 50, 100)),
                    eligible_window(102, frame=frame(0, 0, 50, 100)),
                    eligible_window(201, frame=frame(50, 0, 50, 100), has_focus=True),
                ],
                focused_window_id=201,
                recent_window_id=101,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([201, 101, 102]),
                state_store=store,
            )

            runtime.on_window_created(201)

            stack_actions = [a for a in client.actions if a[0] == "stack"]
            self.assertEqual(stack_actions, [])
            self.assertIsNone(store.read().spaces["1:2"].pending_split)

    def test_on_window_created_exits_when_window_destroyed(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            store.write(RuntimeState(spaces={workflow_space.storage_key: tracked_space(workflow_space)}))
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, frame=frame(0, 0, 100, 100), has_focus=True),
                ],
                focused_window_id=101,
                recent_window_id=101,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([101]),
                state_store=store,
            )

            runtime.on_window_created(999)

            stack_actions = [a for a in client.actions if a[0] == "stack"]
            self.assertEqual(stack_actions, [])

class CliTests(unittest.TestCase):
    def test_alttab_command_dispatches_runtime_with_parsed_frames(self) -> None:
        runtime = MagicMock()
        with patch.object(cli_module, "SubprocessYabaiClient", return_value=object()):
            with patch.object(cli_module, "SubprocessHammerspoonClient", return_value=object()):
                with patch.object(cli_module, "WorkflowRuntime", return_value=runtime):
                    result = cli_module.main(
                        [
                            "alttab",
                            "--origin-window-id",
                            "101",
                            "--selected-window-id",
                            "202",
                            "--origin-open-frame",
                            "0,0,50,100",
                            "--selected-open-frame",
                            "50,0,50,100",
                            "--selected-was-visible-at-open",
                        ]
                    )

        self.assertEqual(result, 0)
        runtime.alttab.assert_called_once_with(
            origin_window_id=101,
            selected_window_id=202,
            origin_open_frame=NormalizedFrame(x=0, y=0, w=50, h=100),
            selected_open_frame=NormalizedFrame(x=50, y=0, w=50, h=100),
            selected_was_visible_at_open=True,
        )

    def test_on_window_created_command_dispatches_runtime(self) -> None:
        runtime = MagicMock()
        with patch.object(cli_module, "SubprocessYabaiClient", return_value=object()):
            with patch.object(cli_module, "SubprocessHammerspoonClient", return_value=object()):
                with patch.object(cli_module, "WorkflowRuntime", return_value=runtime):
                    result = cli_module.main(
                        ["on-window-created", "--window-id", "12345"]
                    )
        self.assertEqual(result, 0)
        runtime.on_window_created.assert_called_once_with(12345)

    def test_install_hammerspoon_dispatches_install(self) -> None:
        with patch.object(cli_module, "SubprocessYabaiClient", return_value=object()):
            with patch.object(cli_module, "SubprocessHammerspoonClient", return_value=object()):
                with patch.object(cli_module, "install_hammerspoon") as install:
                    result = cli_module.main(["install", "hammerspoon"])

        self.assertEqual(result, 0)
        install.assert_called_once()


class IntegrationInstallTests(unittest.TestCase):
    def test_install_hammerspoon_only_manages_windy_block_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            runtime_root = Path(tempdir) / "runtime"
            module_path = runtime_root / "hammerspoon" / "windy.lua"
            module_path.parent.mkdir(parents=True)
            module_path.write_text("return {}\n", encoding="utf-8")

            fake_home = Path(tempdir) / "home"
            init_path = fake_home / ".hammerspoon" / "init.lua"
            init_path.parent.mkdir(parents=True)
            init_path.write_text(
                'require("hs.ipc")\n\n'
                '-- BEGIN YHWM_RUNTIME_V2\nold block\n-- END YHWM_RUNTIME_V2\n\n'
                '-- BEGIN YHWM_RUNTIME\nnew block\n-- END YHWM_RUNTIME\n',
                encoding="utf-8",
            )

            with patch("pathlib.Path.home", return_value=fake_home):
                with patch("subprocess.run", return_value=CompletedProcessStub(0)):
                    install_hammerspoon(
                        runtime_root=runtime_root,
                        executable_path="/tmp/runtime/bin/windy",
                        yabai_path="/opt/homebrew/bin/yabai",
                        hs_bin="/opt/homebrew/bin/hs",
                    )
                    install_hammerspoon(
                        runtime_root=runtime_root,
                        executable_path="/tmp/runtime/bin/windy",
                        yabai_path="/opt/homebrew/bin/yabai",
                        hs_bin="/opt/homebrew/bin/hs",
                    )

            final_text = init_path.read_text(encoding="utf-8")
            self.assertEqual(final_text.count(INIT_BLOCK_START), 1)
            self.assertEqual(final_text.count(INIT_BLOCK_END), 1)
            self.assertIn("BEGIN YHWM_RUNTIME_V2", final_text)
            self.assertIn("BEGIN YHWM_RUNTIME", final_text)
            self.assertIn("/tmp/runtime/bin/windy", final_text)
            self.assertIn('/opt/homebrew/bin/yabai', final_text)
            self.assertIn('hs.task.new("/opt/homebrew/bin/yabai"', final_text)
            self.assertIn('"--restart-service"', final_text)
            self.assertIn("start_windy()", final_text)


class SubprocessHammerspoonClientTests(unittest.TestCase):
    def test_ordered_window_ids_parses_json(self) -> None:
        client = SubprocessHammerspoonClient("/opt/homebrew/bin/hs")
        with patch(
            "subprocess.run",
            return_value=CompletedProcessStub(0, stdout="[101, 202]\n"),
        ):
            self.assertEqual(client.ordered_window_ids(), [101, 202])

    def test_ordered_window_ids_raises_on_invalid_json(self) -> None:
        client = SubprocessHammerspoonClient("/opt/homebrew/bin/hs")
        with patch(
            "subprocess.run",
            return_value=CompletedProcessStub(0, stdout="not json\n"),
        ):
            with self.assertRaises(WorkflowError):
                client.ordered_window_ids()


class SubprocessYabaiClientTests(unittest.TestCase):
    def test_focus_window_direction_swallows_missing_neighbor_error(self) -> None:
        client = SubprocessYabaiClient("/opt/homebrew/bin/yabai")
        with patch(
            "subprocess.run",
            return_value=CompletedProcessStub(
                1,
                stderr="could not locate a westward managed window.\n",
            ),
        ):
            client.focus_window_direction("west")

    def test_focus_window_direction_raises_on_other_errors(self) -> None:
        client = SubprocessYabaiClient("/opt/homebrew/bin/yabai")
        with patch(
            "subprocess.run",
            return_value=CompletedProcessStub(1, stderr="unexpected failure\n"),
        ):
            with self.assertRaises(WorkflowError):
                client.focus_window_direction("west")


class CompletedProcessStub:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeHammerspoonClient:
    def __init__(self, ordered_ids: list[int]):
        self._ordered_ids = list(ordered_ids)

    def ordered_window_ids(self) -> list[int]:
        return list(self._ordered_ids)


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
        directional_focus_targets: dict[tuple[int, str], int] | None = None,
    ):
        self._windows = {int(window["id"]): dict(window) for window in windows}
        self._focused_window_id = focused_window_id
        self._recent_window_id = recent_window_id
        self._focus_follows_mouse = focus_follows_mouse
        self._mouse_follows_focus = mouse_follows_focus
        self._layout_by_space = layout_by_space or {}
        self._directional_focus_targets = directional_focus_targets or {}
        self.actions: list[tuple] = []

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

    def focus_window_direction(self, direction: str) -> None:
        self.actions.append(("focus_direction", direction))
        target_window_id = self._directional_focus_targets.get((self._focused_window_id, direction))
        if target_window_id is not None:
            self._set_focus(target_window_id)

    def swap_window(self, window_id: int, target_window_id: int) -> None:
        self.actions.append(("swap", window_id, target_window_id))

    def rediscover_window(self, window_id: int) -> bool:
        self.actions.append(("rediscover", window_id))
        return window_id in self._windows

    def _set_focus(self, window_id: int) -> None:
        self._focused_window_id = window_id
        self._recent_window_id = window_id
        for current_window_id, window in self._windows.items():
            window["has-focus"] = current_window_id == window_id


def tracked_space(
    workflow_space: EligibleWorkflowSpace,
    *,
    pending_split: PendingSplit | None = None,
) -> TrackedSpaceState:
    return TrackedSpaceState(
        workflow_space=workflow_space,
        pending_split=pending_split,
    )


def frame(x: int, y: int, w: int, h: int) -> dict[str, float]:
    return {"x": float(x), "y": float(y), "w": float(w), "h": float(h)}


def eligible_window(
    window_id: int,
    *,
    frame: dict[str, float],
    display: int = 1,
    space: int = 2,
    app: str = "TextEdit",
    title: str = "window",
    has_focus: bool = False,
) -> dict:
    return {
        "id": window_id,
        "app": app,
        "title": title,
        "display": display,
        "space": space,
        "frame": frame,
        "root-window": True,
        "role": "AXWindow",
        "subrole": "AXStandardWindow",
        "can-move": True,
        "has-ax-reference": True,
        "has-focus": has_focus,
        "layer": "normal",
        "level": 0,
        "is-native-fullscreen": False,
        "is-visible": True,
        "is-minimized": False,
        "is-hidden": False,
        "is-floating": False,
        "is-sticky": False,
    }
