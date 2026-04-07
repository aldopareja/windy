# Window Creation Handling v2 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** New windows in tracked spaces stack into the focused tile via yabai signal-based absorption. No `--insert stack` arming (which caused a persistent red visual overlay). No Hammerspoon `windowFilter` watcher (replaced by faster yabai signal).

**Architecture:** A yabai signal registered at reseed time fires on window creation. The signal invokes `windy on-window-created` which snapshots the entire space and absorbs all solo tiles (windows that yabai auto-split) into the anchor tile (the tile with the most windows). Ghost windows are rediscovered via the already-deployed `--rediscover` yabai patch.

**Tech Stack:** Python (windy CLI/workflow), Lua (Hammerspoon), unittest

**Spec:** `docs/specs/2026-04-07-window-creation-handling-design.md`

**Test runner:** `cd /Users/aldo/cwd_v2/windy && python3 -m unittest tests.test_windy -v`

**Baseline:** 28 tests currently pass. The v1 implementation has `arm_window_stack` calls and a Hammerspoon `windowFilter` watcher that this plan replaces.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `windy/windy/yabai.py` | Modify | Remove `arm_window_stack`, add `add_signal`/`remove_signal` |
| `windy/windy/workflow.py` | Modify | Add `windy_bin` param, signal lifecycle, rewrite `on_window_created`, add `_find_anchor_tile` |
| `windy/windy/cli.py` | Modify | Pass `windy_bin` to `WorkflowRuntime` |
| `windy/hammerspoon/windy.lua` | Modify | Remove `windowFilter` watcher and its cleanup |
| `windy/tests/test_windy.py` | Modify | Update `FakeYabaiClient`, all `WorkflowRuntime` constructions, test expectations |

---

### Task 1: Add signal methods to yabai client

**Files:**
- Modify: `windy/windy/yabai.py`
- Modify: `windy/tests/test_windy.py`

- [ ] **Step 1: Add `add_signal` and `remove_signal` to `YabaiClient` Protocol**

In `windy/windy/yabai.py`, add after `swap_window` (after line 53):

```python
    def add_signal(self, *, label: str, event: str, action: str) -> None:
        ...

    def remove_signal(self, label: str) -> None:
        ...
```

- [ ] **Step 2: Add implementations to `SubprocessYabaiClient`**

In `windy/windy/yabai.py`, add after `swap_window` implementation (after line 191):

```python
    def add_signal(self, *, label: str, event: str, action: str) -> None:
        self._run_text(
            ["-m", "signal", "--add", f"label={label}", f"event={event}", f"action={action}"],
            error_context=f"Failed to add yabai signal '{label}'",
        )

    def remove_signal(self, label: str) -> None:
        self._run_text(
            ["-m", "signal", "--remove", label],
            error_context=f"Failed to remove yabai signal '{label}'",
        )
```

- [ ] **Step 3: Add methods to `FakeYabaiClient` in tests**

In `windy/tests/test_windy.py`, add to `FakeYabaiClient` (after `rediscover_window`):

```python
    def add_signal(self, *, label: str, event: str, action: str) -> None:
        self.actions.append(("add_signal", label, event))

    def remove_signal(self, label: str) -> None:
        self.actions.append(("remove_signal", label))
```

- [ ] **Step 4: Run tests**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -m unittest tests.test_windy -v
```

Expected: All 28 tests pass (purely additive, no behavior change).

- [ ] **Step 5: Commit**

```bash
cd /Users/aldo/cwd_v2/windy
git add windy/yabai.py tests/test_windy.py
git commit -m "Add add_signal and remove_signal to yabai client"
```

---

### Task 2: Add `windy_bin` to `WorkflowRuntime` and update `cli.py`

**Files:**
- Modify: `windy/windy/workflow.py`
- Modify: `windy/windy/cli.py`
- Modify: `windy/tests/test_windy.py`

- [ ] **Step 1: Add `windy_bin` parameter to `WorkflowRuntime.__init__`**

In `windy/windy/workflow.py`, change the constructor (lines 49-58):

```python
class WorkflowRuntime:
    def __init__(
        self,
        *,
        yabai: YabaiClient,
        hammerspoon: HammerspoonClient,
        state_store: RuntimeStateStore,
        windy_bin: str,
    ):
        self._yabai = yabai
        self._hammerspoon = hammerspoon
        self._state_store = state_store
        self._windy_bin = windy_bin
```

- [ ] **Step 2: Update `cli.py` to pass `windy_bin`**

In `windy/windy/cli.py`, change the `WorkflowRuntime` construction (around line 114):

```python
    windy_bin = str(Path(__file__).resolve().parents[1] / "bin" / "windy")
    runtime = WorkflowRuntime(yabai=yabai, hammerspoon=hammerspoon, state_store=state_store, windy_bin=windy_bin)
```

- [ ] **Step 3: Update ALL `WorkflowRuntime` constructions in tests**

In `windy/tests/test_windy.py`, add `windy_bin="/test/bin/windy"` to EVERY `WorkflowRuntime(...)` call. There are 15 instances. Each must become:

```python
            runtime = WorkflowRuntime(
                yabai=client,
                hammerspoon=FakeHammerspoonClient([...]),
                state_store=store,
                windy_bin="/test/bin/windy",
            )
```

Search for `WorkflowRuntime(` in the test file and update every instance. (CLI tests that mock `WorkflowRuntime` entirely via `patch.object` do not need `windy_bin`.)

- [ ] **Step 4: Run tests**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -m unittest tests.test_windy -v
```

Expected: All 28 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/aldo/cwd_v2/windy
git add windy/workflow.py windy/cli.py tests/test_windy.py
git commit -m "Add windy_bin parameter to WorkflowRuntime"
```

---

### Task 3: Replace `arm_window_stack` with signal lifecycle

**Files:**
- Modify: `windy/windy/workflow.py`
- Modify: `windy/windy/yabai.py`
- Modify: `windy/tests/test_windy.py`

- [ ] **Step 1: Write failing test — reseed registers signal**

In `windy/tests/test_windy.py`, add:

```python
    def test_reseed_registers_absorb_signal(self) -> None:
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
                windy_bin="/test/bin/windy",
            )

            runtime.reseed()

            self.assertIn(("remove_signal", "windy_absorb"), client.actions)
            signal_adds = [a for a in client.actions if a[0] == "add_signal"]
            self.assertEqual(len(signal_adds), 1)
            self.assertEqual(signal_adds[0][1], "windy_absorb")
            self.assertEqual(signal_adds[0][2], "window_created")
```

- [ ] **Step 2: Write failing test — float removes signal when last tracked space**

```python
    def test_float_removes_signal_when_last_tracked_space(self) -> None:
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
                windy_bin="/test/bin/windy",
            )

            runtime.float_space()

            self.assertIn(("remove_signal", "windy_absorb"), client.actions)
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -m unittest tests.test_windy.WorkflowRuntimeTests.test_reseed_registers_absorb_signal tests.test_windy.WorkflowRuntimeTests.test_float_removes_signal_when_last_tracked_space -v
```

Expected: Both FAIL.

- [ ] **Step 4: Implement signal lifecycle in `reseed()` and `float_space()`**

In `windy/windy/workflow.py`, modify `reseed()` — replace line 101 (`self._yabai.arm_window_stack(target.focused_window_id)`) and add signal registration after the state write (after line 111):

```python
        self._state_store.write(
            _replace_space_state(
                state,
                TrackedSpaceState(
                    workflow_space=target.workflow_space,
                    pending_split=None,
                ),
            )
        )

        try:
            self._yabai.remove_signal("windy_absorb")
        except WorkflowError:
            pass
        self._yabai.add_signal(
            label="windy_absorb",
            event="window_created",
            action=f"{self._windy_bin} on-window-created --window-id $YABAI_WINDOW_ID",
        )
```

Remove the `self._yabai.arm_window_stack(target.focused_window_id)` line entirely.

Modify `float_space()` — after `self._state_store.write(...)` (line 205), add signal removal:

```python
        self._yabai.set_space_layout(context.workflow_space.space, "float")
        updated_state = _delete_space_state(context.state, context.workflow_space)
        self._state_store.write(updated_state)
        if not updated_state.spaces:
            try:
                self._yabai.remove_signal("windy_absorb")
            except WorkflowError:
                pass
```

- [ ] **Step 5: Remove `arm_window_stack` from `split()` and `delete_tile()`**

In `split()`, remove line 158: `self._yabai.arm_window_stack(focused_tile.visible_window_id)`

In `delete_tile()`, remove line 191: `self._yabai.arm_window_stack(anchor_tile.visible_window_id)`

- [ ] **Step 6: Remove `arm_window_stack` from `YabaiClient` Protocol and `SubprocessYabaiClient`**

In `windy/windy/yabai.py`, remove from Protocol:
```python
    def arm_window_stack(self, window_id: int) -> None:
        ...
```

Remove from `SubprocessYabaiClient`:
```python
    def arm_window_stack(self, window_id: int) -> None:
        self._run_text(
            ["-m", "window", str(window_id), "--insert", "stack"],
            error_context=f"Failed to arm stack insertion on window {window_id}",
        )
```

- [ ] **Step 7: Remove `arm_window_stack` from `FakeYabaiClient` and delete arm_stack tests**

In `windy/tests/test_windy.py`:

Remove from `FakeYabaiClient`:
```python
    def arm_window_stack(self, window_id: int) -> None:
        self.actions.append(("arm_stack", window_id))
```

Delete these 3 test methods entirely:
- `test_reseed_arms_stack_insertion_on_focused_window`
- `test_split_promote_arms_stack_insertion`
- `test_delete_tile_arms_stack_insertion`

- [ ] **Step 8: Update existing test expectations**

In `test_reseed_tracks_space_and_stacks_other_windows`, update expected actions — remove `("arm_stack", 101)` and add signal actions:

```python
            self.assertEqual(
                client.actions,
                [
                    ("set_layout", 2, "bsp"),
                    ("stack", 101, 102),
                    ("stack", 101, 103),
                    ("focus", 101),
                    ("remove_signal", "windy_absorb"),
                    ("add_signal", "windy_absorb", "window_created"),
                ],
            )
```

In `test_split_with_background_in_focused_tile_promotes_candidate`, remove `("arm_stack", 101)`:

```python
            self.assertEqual(client.actions, [("promote", 102, "east"), ("focus", 101)])
```

In `test_delete_tile_merges_focused_tile_into_recent_sibling`, remove `("arm_stack", 201)`:

```python
            self.assertEqual(
                client.actions,
                [
                    ("stack", 201, 101),
                    ("stack", 201, 102),
                    ("focus", 201),
                ],
            )
```

In `test_float_clears_tracking`, add `("remove_signal", "windy_absorb")` to expected actions (float_space now removes the signal when no tracked spaces remain):

```python
            self.assertEqual(client.actions, [("set_layout", 2, "float"), ("remove_signal", "windy_absorb")])
```

- [ ] **Step 9: Run all tests**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -m unittest tests.test_windy -v
```

Expected: All tests pass (28 - 3 removed + 2 added = 27).

- [ ] **Step 10: Commit**

```bash
cd /Users/aldo/cwd_v2/windy
git add windy/yabai.py windy/workflow.py tests/test_windy.py
git commit -m "Replace arm_window_stack with yabai signal lifecycle

Register windy_absorb signal at reseed, remove at float when no
tracked spaces remain. Removes --insert stack arming which caused
a persistent red visual overlay."
```

---

### Task 4: Rewrite `on_window_created` with space-level absorption

**Files:**
- Modify: `windy/windy/workflow.py`
- Modify: `windy/tests/test_windy.py`

- [ ] **Step 1: Write failing test — absorbs single solo tile**

Replace the existing `test_on_window_created_absorbs_unwanted_split`:

```python
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
                windy_bin="/test/bin/windy",
            )

            runtime.on_window_created(201)

            self.assertIn(("stack", 101, 201), client.actions)
            self.assertIn(("focus", 201), client.actions)
```

- [ ] **Step 2: Write failing test — absorbs multiple solo tiles**

```python
    def test_on_window_created_absorbs_multiple_solo_tiles(self) -> None:
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
                windy_bin="/test/bin/windy",
            )

            runtime.on_window_created(301)

            stack_actions = [a for a in client.actions if a[0] == "stack"]
            stacked_ids = {a[2] for a in stack_actions}
            self.assertEqual(stacked_ids, {201, 301})
```

- [ ] **Step 3: Write failing test — idempotent when no solo tiles**

```python
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
                windy_bin="/test/bin/windy",
            )

            runtime.on_window_created(102)

            stack_actions = [a for a in client.actions if a[0] == "stack"]
            self.assertEqual(stack_actions, [])
```

- [ ] **Step 4: Update existing `test_on_window_created_preserves_intentional_split`**

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
                windy_bin="/test/bin/windy",
            )

            runtime.on_window_created(201)

            stack_actions = [a for a in client.actions if a[0] == "stack"]
            self.assertEqual(stack_actions, [])
            self.assertIsNone(store.read().spaces["1:2"].pending_split)
```

- [ ] **Step 5: Write test — window destroyed before signal fires**

```python
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
                windy_bin="/test/bin/windy",
            )

            runtime.on_window_created(999)

            stack_actions = [a for a in client.actions if a[0] == "stack"]
            self.assertEqual(stack_actions, [])
```

- [ ] **Step 6: Delete old `test_on_window_created_noop_when_already_stacked`**

Remove this test entirely — replaced by `test_on_window_created_idempotent_when_no_solo_tiles`.

- [ ] **Step 7: Implement `_find_anchor_tile` helper**

In `windy/windy/workflow.py`, add after `_choose_delete_anchor_tile` (after line 522):

```python
def _find_anchor_tile(snapshot: _LiveSpaceSnapshot) -> Optional[LiveTile]:
    if not snapshot.tiles:
        return None
    return max(snapshot.tiles, key=lambda t: len(t.all_window_ids))
```

- [ ] **Step 8: Rewrite `on_window_created` method**

Replace the entire `on_window_created` method (lines 280-346) with:

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
        pending_split_consumed = had_pending_split and tracked.pending_split is None

        anchor_tile = _find_anchor_tile(snapshot)
        if anchor_tile is None:
            return

        absorbed = []
        for tile in snapshot.tiles:
            if tile.frame == anchor_tile.frame:
                continue
            if len(tile.all_window_ids) != 1:
                continue
            solo_id = tile.visible_window_id
            if pending_split_consumed and solo_id == window_id:
                continue
            self._yabai.stack_window(anchor_tile.visible_window_id, solo_id)
            absorbed.append(solo_id)

        if window_id in absorbed:
            self._yabai.focus_window(window_id)
```

- [ ] **Step 9: Run all tests**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -m unittest tests.test_windy -v
```

Expected: All tests pass (27 - 2 removed + 3 added = 28). Count may vary slightly based on updates in Task 3 — verify total and that all pass.

- [ ] **Step 10: Commit**

```bash
cd /Users/aldo/cwd_v2/windy
git add windy/workflow.py tests/test_windy.py
git commit -m "Rewrite on_window_created with space-level absorption

Absorbs ALL solo tiles into the anchor (tile with most windows)
in a single pass. Handles both single cmd+N and bulk app launches
through the same code path. Idempotent for concurrent signals."
```

---

### Task 5: Remove Hammerspoon `windowFilter` watcher

**Files:**
- Modify: `windy/hammerspoon/windy.lua`

- [ ] **Step 1: Remove `windowFilter` cleanup from `stopExisting()`**

In `windy/hammerspoon/windy.lua`, remove these lines from `stopExisting()`:

```lua
  if runtimeState.windowFilter ~= nil then
    runtimeState.windowFilter:unsubscribeAll()
    runtimeState.windowFilter = nil
  end
```

- [ ] **Step 2: Remove `windowFilter` subscription from `module.start()`**

Remove the entire block after `state.flagsTap:start()`:

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

- [ ] **Step 3: Reload Hammerspoon**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -c "from windy.cli import main; main(['install', 'hammerspoon'])"
```

- [ ] **Step 4: Commit**

```bash
cd /Users/aldo/cwd_v2/windy
git add hammerspoon/windy.lua
git commit -m "Remove Hammerspoon windowFilter watcher

Replaced by yabai signal registered at reseed time. The signal
fires immediately after BSP insertion (same event loop tick),
much faster than the 300ms Hammerspoon delay."
```

---

### Task 6: Live end-to-end verification

- [ ] **Step 1: Run full test suite**

```bash
cd /Users/aldo/cwd_v2/windy && python3 -m unittest tests.test_windy -v
```

Expected: All tests pass.

- [ ] **Step 2: Reseed and verify signal is registered**

```bash
cd /Users/aldo/cwd_v2/windy
python3 -c "from windy.cli import main; main(['reseed'])"
sleep 0.5
yabai -m signal --list 2>/dev/null | python3 -c "
import sys, json
signals = json.load(sys.stdin)
windy_signals = [s for s in signals if s.get('label') == 'windy_absorb']
if windy_signals:
    print(f'Signal registered: event={windy_signals[0][\"event\"]} action={windy_signals[0][\"action\"][:60]}...')
else:
    print('ERROR: windy_absorb signal not found!')
"
```

Expected: `Signal registered: event=window_created action=...`

- [ ] **Step 3: Verify no red overlay after reseed**

Visual check: after reseeding, there should be NO red border/overlay on any window. The space should show a clean single tile with all windows stacked.

- [ ] **Step 4: Test terminal cmd+N — should stack**

```bash
# Count tiles before
echo "=== Before ==="
yabai -m query --windows --space | python3 -c "
import sys, json; ws = json.load(sys.stdin)
managed = [w for w in ws if not w['is-floating']]
frames = set(f'{w[\"frame\"][\"x\"]:.0f},{w[\"frame\"][\"y\"]:.0f}' for w in managed)
print(f'{len(managed)} windows, {len(frames)} tiles')
"

# Open new terminal
osascript -e 'tell application "System Events" to keystroke "n" using command down'
sleep 1

echo "=== After cmd+N ==="
yabai -m query --windows --space | python3 -c "
import sys, json; ws = json.load(sys.stdin)
managed = [w for w in ws if not w['is-floating']]
frames = set(f'{w[\"frame\"][\"x\"]:.0f},{w[\"frame\"][\"y\"]:.0f}' for w in managed)
print(f'{len(managed)} windows, {len(frames)} tiles')
"
```

Expected: Window count increases by 1, tile count stays at 1.

- [ ] **Step 5: Clean up test window and test intentional split**

```bash
# Close test window
osascript -e 'tell application "System Events" to keystroke "w" using command down'
sleep 0.5

# Reseed again
python3 -c "from windy.cli import main; main(['reseed'])"
sleep 0.5

# Intentional split
python3 -c "from windy.cli import main; main(['split', '--direction', 'east'])"
sleep 0.5

# Open new window — should land in the split, not be absorbed
osascript -e 'tell application "System Events" to keystroke "n" using command down'
sleep 1

echo "=== After intentional split + cmd+N ==="
yabai -m query --windows --space | python3 -c "
import sys, json; ws = json.load(sys.stdin)
managed = [w for w in ws if not w['is-floating']]
frames = set(f'{w[\"frame\"][\"x\"]:.0f},{w[\"frame\"][\"y\"]:.0f}' for w in managed)
print(f'{len(managed)} windows, {len(frames)} tiles')
"
```

Expected: 2 tiles (the intentional split was preserved).

- [ ] **Step 6: Test Zen browser cmd+N — should rediscover and stack**

```bash
# Close test window, reseed
osascript -e 'tell application "System Events" to keystroke "w" using command down'
sleep 0.5
python3 -c "from windy.cli import main; main(['reseed'])"
sleep 0.5

# Focus Zen and cmd+N
yabai -m window --focus $(yabai -m query --windows --space | python3 -c "import sys,json; ws=json.load(sys.stdin); zen=[w for w in ws if w['app']=='Zen']; print(zen[0]['id'] if zen else '')")
sleep 0.3
osascript -e 'tell application "System Events" to keystroke "n" using command down'
sleep 2

echo "=== After Zen cmd+N ==="
yabai -m query --windows --space | python3 -c "
import sys, json; ws = json.load(sys.stdin)
managed = [w for w in ws if not w['is-floating']]
frames = set(f'{w[\"frame\"][\"x\"]:.0f},{w[\"frame\"][\"y\"]:.0f}' for w in managed)
print(f'{len(managed)} windows, {len(frames)} tiles')
"
yabai -m query --windows | python3 -c "
import sys, json
for w in json.load(sys.stdin):
    if w['app'].lower() == 'zen':
        print(f'  {w[\"app\"]}({w[\"id\"]}) subrole={w.get(\"subrole\",\"?\")} has-ax={w.get(\"has-ax-reference\",\"?\")}')
"
```

Expected: 1 tile. All Zen windows show `subrole=AXStandardWindow has-ax=True`.
