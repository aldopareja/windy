# Window Creation Handling — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** New windows in tracked spaces stack into the focused tile instead of creating BSP splits. Ghost windows (apps with delayed AX initialization) are rediscovered and managed by yabai.

**Architecture:** Three layers: (1) a patched yabai with `--rediscover` command, (2) `--insert stack` arming after every stable windy command, (3) a Hammerspoon `hs.window.filter` watcher that triggers `windy on-window-created` to reconcile and re-arm.

**Tech Stack:** C (yabai patch), Python (windy CLI/workflow), Lua (Hammerspoon), unittest (tests)

**Spec:** `docs/specs/2026-04-07-window-creation-handling-design.md`

**Test runner:** `python3 -m unittest tests.test_windy -v` (from `windy/` directory)

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `github_and_docs_reference/yabai/yabai/src/message.c` | Already modified | `--rediscover` command handler |
| `windy/windy/yabai.py` | Modify | Add `arm_window_stack()`, `rediscover_window()` to Protocol + impl |
| `windy/windy/workflow.py` | Modify | Add `on_window_created()`; modify `reseed()`, `split()`, `delete_tile()` to arm stack |
| `windy/windy/cli.py` | Modify | Add `on-window-created` subcommand |
| `windy/hammerspoon/windy.lua` | Modify | Add `hs.window.filter` watcher + cleanup |
| `windy/tests/test_windy.py` | Modify | Add tests for new behavior; update FakeYabaiClient |

---

### Task 1: Build and deploy patched yabai

**Files:**
- Source: `github_and_docs_reference/yabai/yabai/src/message.c` (already patched)
- Binary: `/opt/homebrew/bin/yabai`

- [ ] **Step 1: Verify the patch compiles**

```bash
cd /Users/aldo/cwd_v2/github_and_docs_reference/yabai/yabai
make install
```

Expected: `bin/yabai` is produced with no errors.

- [ ] **Step 2: Deploy the patched binary**

```bash
yabai --stop-service
cp bin/yabai /opt/homebrew/bin/yabai
yabai --start-service
```

- [ ] **Step 3: Verify `--rediscover` is available**

```bash
yabai -m window --rediscover 999999
```

Expected: exit 1 with error message like `could not rediscover window with id '999999'.` (confirms the command is wired up).

---

### Task 2: Extend FakeYabaiClient and YabaiClient protocol

**Files:**
- Modify: `windy/windy/yabai.py`
- Modify: `windy/tests/test_windy.py`

- [ ] **Step 1: Add `arm_window_stack` and `rediscover_window` to the YabaiClient Protocol**

In `windy/windy/yabai.py`, add two methods to the `YabaiClient` Protocol class (after `swap_window`):

```python
    def arm_window_stack(self, window_id: int) -> None:
        ...

    def rediscover_window(self, window_id: int) -> bool:
        ...
```

- [ ] **Step 2: Add implementations to SubprocessYabaiClient**

In `windy/windy/yabai.py`, add to `SubprocessYabaiClient` (after `swap_window`):

```python
    def arm_window_stack(self, window_id: int) -> None:
        self._run_text(
            ["-m", "window", str(window_id), "--insert", "stack"],
            error_context=f"Failed to arm stack insertion on window {window_id}",
        )

    def rediscover_window(self, window_id: int) -> bool:
        try:
            self._run_text(
                ["-m", "window", "--rediscover", str(window_id)],
                error_context=f"Failed to rediscover window {window_id}",
            )
            return True
        except WorkflowError:
            return False
```

- [ ] **Step 3: Add methods to FakeYabaiClient in tests**

In `windy/tests/test_windy.py`, add to `FakeYabaiClient` (after `swap_window`):

```python
    def arm_window_stack(self, window_id: int) -> None:
        self.actions.append(("arm_stack", window_id))

    def rediscover_window(self, window_id: int) -> bool:
        self.actions.append(("rediscover", window_id))
        return window_id in self._windows
```

- [ ] **Step 4: Run tests to verify nothing breaks**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -m unittest tests.test_windy -v
```

Expected: 20 tests pass (all existing tests unchanged).

- [ ] **Step 5: Commit**

```bash
cd /Users/aldo/cwd_v2/windy
git add windy/yabai.py tests/test_windy.py
git commit -m "Add arm_window_stack and rediscover_window to yabai client"
```

---

### Task 3: Arm `--insert stack` after reseed, split, and delete_tile

**Files:**
- Modify: `windy/windy/workflow.py`
- Modify: `windy/tests/test_windy.py`

- [ ] **Step 1: Write failing test — reseed arms stack insertion**

In `windy/tests/test_windy.py`, add after `test_reseed_tracks_space_and_stacks_other_windows`:

```python
    def test_reseed_arms_stack_insertion_on_focused_window(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = RuntimeStateStore(Path(tempdir) / "state.json")
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

            runtime.reseed()

            self.assertIn(("arm_stack", 101), client.actions)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -m unittest tests.test_windy.WorkflowRuntimeTests.test_reseed_arms_stack_insertion_on_focused_window -v
```

Expected: FAIL — `("arm_stack", 101)` not in actions.

- [ ] **Step 3: Modify reseed() to arm stack insertion**

In `windy/windy/workflow.py`, add one line after `self._yabai.focus_window(target.focused_window_id)` (line 100) and before the `self._state_store.write(...)` call:

```python
        self._yabai.arm_window_stack(target.focused_window_id)
```

- [ ] **Step 4: Update existing reseed test to expect arm_stack action**

In `test_reseed_tracks_space_and_stacks_other_windows`, update the expected actions to include the new arm_stack call:

```python
            self.assertEqual(
                client.actions,
                [
                    ("set_layout", 2, "bsp"),
                    ("stack", 101, 102),
                    ("stack", 101, 103),
                    ("focus", 101),
                    ("arm_stack", 101),
                ],
            )
```

- [ ] **Step 5: Run tests to verify reseed tests pass**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -m unittest tests.test_windy.WorkflowRuntimeTests.test_reseed_arms_stack_insertion_on_focused_window -v
```

Expected: PASS.

- [ ] **Step 5: Write failing test — split promote path arms stack insertion**

```python
    def test_split_promote_arms_stack_insertion(self) -> None:
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

            runtime.split("east")

            self.assertIn(("arm_stack", 101), client.actions)
```

- [ ] **Step 6: Modify split() to arm stack insertion after promote**

In `windy/windy/workflow.py`, add one line after `self._yabai.focus_window(focused_tile.visible_window_id)` (line 156) and before the `self._state_store.write(...)` call:

```python
        self._yabai.arm_window_stack(focused_tile.visible_window_id)
```

- [ ] **Step 7: Update existing split promote test to expect arm_stack action**

In `test_split_with_background_in_focused_tile_promotes_candidate`, update the expected actions:

```python
            self.assertEqual(client.actions, [("promote", 102, "east"), ("focus", 101), ("arm_stack", 101)])
```

- [ ] **Step 8: Run test to verify it passes**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -m unittest tests.test_windy.WorkflowRuntimeTests.test_split_promote_arms_stack_insertion -v
```

Expected: PASS.

- [ ] **Step 8: Write failing test — delete_tile arms stack insertion**

```python
    def test_delete_tile_arms_stack_insertion(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            store.write(RuntimeState(spaces={workflow_space.storage_key: tracked_space(workflow_space)}))
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, frame=frame(0, 0, 50, 100), has_focus=True),
                    eligible_window(201, frame=frame(50, 0, 50, 100)),
                ],
                focused_window_id=101,
                recent_window_id=201,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([101, 201]),
                state_store=store,
            )

            runtime.delete_tile()

            self.assertIn(("arm_stack", 201), client.actions)
```

- [ ] **Step 9: Modify delete_tile() to arm stack insertion**

In `windy/windy/workflow.py`, add one line after `self._yabai.focus_window(anchor_tile.visible_window_id)` (line 188):

```python
        self._yabai.arm_window_stack(anchor_tile.visible_window_id)
```

- [ ] **Step 10: Update existing delete_tile test to expect arm_stack action**

In `test_delete_tile_merges_focused_tile_into_recent_sibling`, update the expected actions:

```python
            self.assertEqual(
                client.actions,
                [
                    ("stack", 201, 101),
                    ("stack", 201, 102),
                    ("focus", 201),
                    ("arm_stack", 201),
                ],
            )
```

- [ ] **Step 11: Run all tests**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -m unittest tests.test_windy -v
```

Expected: All 23 tests pass (20 existing with 3 updated expectations + 3 new).

- [ ] **Step 12: Commit**

```bash
cd /Users/aldo/cwd_v2/windy
git add windy/workflow.py tests/test_windy.py
git commit -m "Arm --insert stack after reseed, split promote, and delete_tile"
```

---

### Task 4: Add `on_window_created` workflow method

**Files:**
- Modify: `windy/windy/workflow.py`
- Modify: `windy/tests/test_windy.py`

- [ ] **Step 1: Write failing test — untracked space is a no-op**

```python
    def test_on_window_created_exits_for_untracked_space(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = RuntimeStateStore(Path(tempdir) / "state.json")
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

            runtime.on_window_created(101)

            self.assertEqual(client.actions, [("rediscover", 101)])
```

- [ ] **Step 2: Write failing test — new window creating unwanted split is absorbed**

```python
    def test_on_window_created_absorbs_unwanted_split(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            store.write(RuntimeState(spaces={workflow_space.storage_key: tracked_space(workflow_space)}))
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, frame=frame(0, 0, 50, 100), has_focus=True),
                    eligible_window(201, frame=frame(50, 0, 50, 100)),
                ],
                focused_window_id=101,
                recent_window_id=101,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([101, 201]),
                state_store=store,
            )

            runtime.on_window_created(201)

            self.assertIn(("stack", 101, 201), client.actions)
            self.assertIn(("focus", 201), client.actions)
            self.assertIn(("arm_stack", 201), client.actions)
```

- [ ] **Step 3: Write failing test — intentional split is preserved**

```python
    def test_on_window_created_preserves_intentional_split(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            store.write(RuntimeState(spaces={
                workflow_space.storage_key: tracked_space(
                    workflow_space,
                    pending_split=PendingSplit(
                        direction="east",
                        anchor_window_id=101,
                        anchor_frame=NormalizedFrame(x=0, y=0, w=100, h=100),
                    ),
                )
            }))
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, frame=frame(0, 0, 50, 100), has_focus=True),
                    eligible_window(201, frame=frame(50, 0, 50, 100)),
                ],
                focused_window_id=101,
                recent_window_id=101,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([101, 201]),
                state_store=store,
            )

            runtime.on_window_created(201)

            stack_actions = [a for a in client.actions if a[0] == "stack"]
            self.assertEqual(stack_actions, [])
            self.assertIn(("arm_stack", 101), client.actions)
            self.assertIsNone(store.read().spaces["1:2"].pending_split)
```

- [ ] **Step 4: Write failing test — already-stacked window is a no-op beyond re-arm**

```python
    def test_on_window_created_noop_when_already_stacked(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workflow_space = EligibleWorkflowSpace(display=1, space=2)
            store = RuntimeStateStore(Path(tempdir) / "state.json")
            store.write(RuntimeState(spaces={workflow_space.storage_key: tracked_space(workflow_space)}))
            client = FakeYabaiClient(
                windows=[
                    eligible_window(101, frame=frame(0, 0, 100, 100), has_focus=True),
                    eligible_window(201, frame=frame(0, 0, 100, 100)),
                ],
                focused_window_id=101,
                recent_window_id=101,
            )
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([101, 201]),
                state_store=store,
            )

            runtime.on_window_created(201)

            stack_actions = [a for a in client.actions if a[0] == "stack"]
            self.assertEqual(stack_actions, [])
            self.assertIn(("arm_stack", 101), client.actions)
```

- [ ] **Step 5: Run tests to verify they all fail**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -m unittest tests.test_windy.WorkflowRuntimeTests.test_on_window_created_exits_for_untracked_space tests.test_windy.WorkflowRuntimeTests.test_on_window_created_absorbs_unwanted_split tests.test_windy.WorkflowRuntimeTests.test_on_window_created_preserves_intentional_split tests.test_windy.WorkflowRuntimeTests.test_on_window_created_noop_when_already_stacked -v
```

Expected: All 4 FAIL — `on_window_created` not defined.

- [ ] **Step 6: Implement on_window_created**

In `windy/windy/workflow.py`, add to `WorkflowRuntime` (after `alttab`):

```python
    def on_window_created(self, window_id: int) -> None:
        self._yabai.rediscover_window(window_id)

        window = _query_window_record_or_none(self._yabai, window_id)
        if window is None:
            return

        workflow_space = _derive_workflow_space_or_none(
            window, description=f"new window {window_id}",
        )
        if workflow_space is None:
            return

        state = self._state_store.read()
        tracked = state.spaces.get(workflow_space.storage_key)
        if tracked is None:
            return

        try:
            validate_workflow_space(
                self._yabai,
                workflow_space=workflow_space,
                allowed_layouts=("bsp",),
            )
        except WorkflowError:
            return

        if not is_eligible_window(
            window,
            target_display=workflow_space.display,
            target_space=workflow_space.space,
        ):
            return

        snapshot = self._live_snapshot(workflow_space)

        had_pending_split = tracked.pending_split is not None
        state, tracked = self._reconcile_tracked_space(state, tracked, snapshot)
        if tracked is None:
            return
        pending_split_was_consumed = had_pending_split and tracked.pending_split is None

        focused_window = _query_focused_window_record_or_none(self._yabai)
        if focused_window is None:
            return
        focused_window_id = int(focused_window["id"])

        focused_tile = snapshot.tile_for_window(focused_window_id)
        if focused_tile is None:
            return

        new_tile = snapshot.tile_for_window(window_id)
        already_in_focused_tile = (
            new_tile is not None and new_tile.frame == focused_tile.frame
        )

        if not already_in_focused_tile and not pending_split_was_consumed:
            self._yabai.stack_window(focused_tile.visible_window_id, window_id)
            self._yabai.focus_window(window_id)
            focused_window_id = window_id

        self._yabai.arm_window_stack(focused_window_id)
```

Also add `validate_workflow_space` to the imports at the top if not already there (it is — line 13).

- [ ] **Step 7: Run all 4 new tests**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -m unittest tests.test_windy.WorkflowRuntimeTests.test_on_window_created_exits_for_untracked_space tests.test_windy.WorkflowRuntimeTests.test_on_window_created_absorbs_unwanted_split tests.test_windy.WorkflowRuntimeTests.test_on_window_created_preserves_intentional_split tests.test_windy.WorkflowRuntimeTests.test_on_window_created_noop_when_already_stacked -v
```

Expected: All 4 PASS.

- [ ] **Step 8: Run full test suite**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -m unittest tests.test_windy -v
```

Expected: All tests pass (23 from Task 3 + 4 new = 27).

- [ ] **Step 9: Commit**

```bash
cd /Users/aldo/cwd_v2/windy
git add windy/workflow.py tests/test_windy.py
git commit -m "Add on_window_created to absorb unwanted splits and re-arm stack insertion"
```

---

### Task 5: Add `on-window-created` CLI subcommand

**Files:**
- Modify: `windy/windy/cli.py`
- Modify: `windy/tests/test_windy.py`

- [ ] **Step 1: Write failing test**

In `windy/tests/test_windy.py`, add to `CliTests`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -m unittest tests.test_windy.CliTests.test_on_window_created_command_dispatches_runtime -v
```

Expected: FAIL — unrecognized arguments or missing subcommand.

- [ ] **Step 3: Add the subcommand to cli.py**

In `windy/windy/cli.py`, add after the `alttab_parser` block (after line 97):

```python
    on_window_created_parser = subparsers.add_parser(
        "on-window-created",
        help="Handle a newly created window in a tracked space.",
    )
    on_window_created_parser.add_argument(
        "--window-id",
        required=True,
        help="macOS window server ID of the newly created window.",
    )
```

And in the command dispatch block, add after the `alttab` elif (after line 134):

```python
        elif args.command == "on-window-created":
            runtime.on_window_created(
                _parse_window_id("on-window-created", args.window_id),
            )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -m unittest tests.test_windy.CliTests.test_on_window_created_command_dispatches_runtime -v
```

Expected: PASS.

- [ ] **Step 5: Run full test suite**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -m unittest tests.test_windy -v
```

Expected: All 28 tests pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/aldo/cwd_v2/windy
git add windy/cli.py tests/test_windy.py
git commit -m "Add on-window-created CLI subcommand"
```

---

### Task 6: Add Hammerspoon window creation watcher

**Files:**
- Modify: `windy/hammerspoon/windy.lua`

- [ ] **Step 1: Add window filter cleanup to `stopExisting()`**

In `windy/hammerspoon/windy.lua`, add inside `stopExisting()` (after the hotkeys cleanup block, before `end`):

```lua
  if runtimeState.windowFilter ~= nil then
    runtimeState.windowFilter:unsubscribeAll()
    runtimeState.windowFilter = nil
  end
```

- [ ] **Step 2: Add window filter subscription to `module.start()`**

In `windy/hammerspoon/windy.lua`, add after the `state.flagsTap:start()` line (line 230), before `end`:

```lua
  local wf = hs.window.filter.new(true)
  wf:subscribe(hs.window.filter.windowCreated, function(win)
    if win == nil then return end
    local windowId = win:id()
    if windowId == nil then return end
    hs.timer.doAfter(0.3, function()
      runWindy(state, {"on-window-created", "--window-id", tostring(windowId)})
    end)
  end)
  state.windowFilter = wf
```

- [ ] **Step 3: Reload Hammerspoon and verify**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -c "from windy.cli import main; main(['install', 'hammerspoon'])"
```

Expected: Hammerspoon reloads with the new watcher active.

- [ ] **Step 4: Commit**

```bash
cd /Users/aldo/cwd_v2/windy
git add hammerspoon/windy.lua
git commit -m "Add hs.window.filter watcher for window creation events"
```

---

### Task 7: End-to-end verification

- [ ] **Step 1: Run the full test suite**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -m unittest tests.test_windy -v
```

Expected: All 28 tests pass.

- [ ] **Step 2: Manual test — terminal cmd+N stacks instead of splitting**

1. Focus a terminal window in a tracked (reseeded) space
2. Press ctrl+alt+space to reseed
3. Press cmd+N to open a new terminal window
4. Verify: the new window stacks into the focused tile (no split)
5. Query: `yabai -m query --windows --space | python3 -c "import sys,json; ws=json.load(sys.stdin); frames={}; [frames.setdefault(f'{w[\"frame\"][\"x\"]:.0f},{w[\"frame\"][\"y\"]:.0f},{w[\"frame\"][\"w\"]:.0f},{w[\"frame\"][\"h\"]:.0f}', []).append(w['app']) for w in ws if not w['is-floating']]; print(f'tiles: {len(frames)}')"` — tile count should not have increased

- [ ] **Step 3: Manual test — Zen browser cmd+N is rediscovered and stacked**

1. Focus a Zen browser window in a tracked space
2. Press cmd+N to open a new Zen window
3. Wait ~1 second for the watcher + rediscovery
4. Verify: `yabai -m query --windows | python3 -c "import sys,json; [print(f'id={w[\"id\"]} app={w[\"app\"]} subrole={w.get(\"subrole\",\"?\")}') for w in json.load(sys.stdin) if w['app'].lower()=='zen']"` — all Zen windows should show `subrole=AXStandardWindow`

- [ ] **Step 4: Manual test — intentional split is preserved**

1. Reseed (ctrl+alt+space)
2. Split east (ctrl+alt+v)
3. Open a new window (cmd+N)
4. Verify: the new window appears in the split, not stacked back
