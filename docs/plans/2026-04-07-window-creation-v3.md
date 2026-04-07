# Window Creation Handling v3 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace unreliable yabai signal detection with a single Hammerspoon windowFilter path for absorbing unwanted BSP splits from new windows.

**Architecture:** Hammerspoon `hs.window.filter` detects all window creation, waits 500ms, calls `windy on-window-created --window-id <id>`. The existing absorption logic in `workflow.py` handles rediscovery, eligibility, and stacking. yabai signal lifecycle is removed entirely.

**Tech Stack:** Python 3 (windy CLI), Lua (Hammerspoon), C (yabai --rediscover patch, unchanged)

**Spec:** `docs/specs/2026-04-07-window-creation-v3-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `windy/yabai.py` | Modify | Remove `add_signal` and `remove_signal` from Protocol and implementation |
| `windy/workflow.py` | Modify | Remove signal lifecycle from `reseed()` and `float_space()`, remove `windy_bin` |
| `windy/cli.py` | Modify | Remove `windy_bin` computation and constructor arg |
| `tests/test_windy.py` | Modify | Remove signal assertions/tests, remove `windy_bin` from constructors |
| `hammerspoon/windy.lua` | Modify | Add windowFilter watcher and cleanup |

---

### Task 1: Remove signal methods from yabai client

**Files:**
- Modify: `windy/yabai.py:56-59` (Protocol methods)
- Modify: `windy/yabai.py:201-211` (SubprocessYabaiClient methods)

- [ ] **Step 1: Remove `add_signal` and `remove_signal` from `YabaiClient` Protocol**

In `windy/yabai.py`, delete these lines from the Protocol class:

```python
    def add_signal(self, *, label: str, event: str, action: str) -> None:
        ...

    def remove_signal(self, label: str) -> None:
        ...
```

- [ ] **Step 2: Remove `add_signal` and `remove_signal` from `SubprocessYabaiClient`**

In `windy/yabai.py`, delete these methods from SubprocessYabaiClient:

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

Do NOT run tests yet — `workflow.py` still calls these methods.

---

### Task 2: Remove signal lifecycle from workflow.py

**Files:**
- Modify: `windy/workflow.py:49-62` (constructor)
- Modify: `windy/workflow.py:115-123` (reseed signal block)
- Modify: `windy/workflow.py:217-221` (float_space signal cleanup)

- [ ] **Step 1: Remove `windy_bin` from constructor**

In `windy/workflow.py`, change the `__init__` method from:

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

to:

```python
class WorkflowRuntime:
    def __init__(
        self,
        *,
        yabai: YabaiClient,
        hammerspoon: HammerspoonClient,
        state_store: RuntimeStateStore,
    ):
        self._yabai = yabai
        self._hammerspoon = hammerspoon
        self._state_store = state_store
```

- [ ] **Step 2: Remove signal registration from `reseed()`**

In `windy/workflow.py`, delete the signal block at the end of `reseed()`. Remove these lines:

```python
        try:
            self._yabai.remove_signal("windy_absorb")
        except WorkflowError:
            pass
        self._yabai.add_signal(
            label="windy_absorb",
            event="window_created",
            action=f"sleep 0.3 && {self._windy_bin} on-window-created --window-id $YABAI_WINDOW_ID",
        )
```

After this edit, `reseed()` should end with:

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
```

- [ ] **Step 3: Remove signal cleanup from `float_space()`**

In `windy/workflow.py`, change the end of `float_space()` from:

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

to:

```python
        self._yabai.set_space_layout(context.workflow_space.space, "float")
        updated_state = _delete_space_state(context.state, context.workflow_space)
        self._state_store.write(updated_state)
```

Do NOT run tests yet — `cli.py` and tests still pass `windy_bin`.

---

### Task 3: Remove `windy_bin` from CLI

**Files:**
- Modify: `windy/cli.py:124-130`

- [ ] **Step 1: Remove `windy_bin` computation and constructor arg**

In `windy/cli.py`, change:

```python
    windy_bin = str(Path(__file__).resolve().parents[1] / "bin" / "windy")
    runtime = WorkflowRuntime(
        yabai=yabai,
        hammerspoon=hammerspoon,
        state_store=state_store,
        windy_bin=windy_bin,
    )
```

to:

```python
    runtime = WorkflowRuntime(
        yabai=yabai,
        hammerspoon=hammerspoon,
        state_store=state_store,
    )
```

Do NOT run tests yet — tests still pass `windy_bin` and assert signal actions.

---

### Task 4: Update tests

**Files:**
- Modify: `tests/test_windy.py`

- [ ] **Step 1: Remove `windy_bin` from all WorkflowRuntime constructors**

In `tests/test_windy.py`, delete every occurrence of:

```python
                windy_bin="/test/bin/windy",
```

There are 18 occurrences at lines: 72, 123, 151, 193, 215, 244, 272, 294, 318, 349, 379, 409, 436, 461, 488, 514, 553, 578.

- [ ] **Step 2: Remove signal assertions from reseed test**

In `test_reseed_tracks_space_and_stacks_other_windows`, change the expected actions from:

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

to:

```python
            self.assertEqual(
                client.actions,
                [
                    ("set_layout", 2, "bsp"),
                    ("stack", 101, 102),
                    ("stack", 101, 103),
                    ("focus", 101),
                ],
            )
```

- [ ] **Step 3: Delete `test_reseed_registers_absorb_signal`**

Delete the entire test method `test_reseed_registers_absorb_signal` (lines 201-224).

- [ ] **Step 4: Remove signal assertion from float test**

In `test_float_clears_tracking`, change:

```python
            self.assertEqual(client.actions, [("set_layout", 2, "float"), ("remove_signal", "windy_absorb")])
```

to:

```python
            self.assertEqual(client.actions, [("set_layout", 2, "float")])
```

- [ ] **Step 5: Delete `test_float_removes_signal_when_last_tracked_space`**

Delete the entire test method `test_float_removes_signal_when_last_tracked_space` (lines 280-299).

- [ ] **Step 6: Remove signal methods from `FakeYabaiClient`**

In `FakeYabaiClient`, delete:

```python
    def add_signal(self, *, label: str, event: str, action: str) -> None:
        self.actions.append(("add_signal", label, event))

    def remove_signal(self, label: str) -> None:
        self.actions.append(("remove_signal", label))
```

- [ ] **Step 7: Run all tests**

Run: `cd /Users/aldo/cwd_v2/windy && python -m pytest tests/test_windy.py -v`

Expected: All tests pass. Should be 26 tests (was 28, minus 2 deleted).

- [ ] **Step 8: Commit**

```bash
git add windy/yabai.py windy/workflow.py windy/cli.py tests/test_windy.py
git commit -m "Remove yabai signal lifecycle from windy

Detection moves to Hammerspoon windowFilter (next commit).
Signal methods, windy_bin plumbing, and signal-related tests removed."
```

---

### Task 5: Add Hammerspoon windowFilter watcher

**Files:**
- Modify: `hammerspoon/windy.lua:5-24` (stopExisting)
- Modify: `hammerspoon/windy.lua:149-231` (module.start)

- [ ] **Step 1: Add windowFilter cleanup to `stopExisting()`**

In `hammerspoon/windy.lua`, add a windowFilter cleanup block after the hotkeys cleanup in `stopExisting()`. Change:

```lua
  if runtimeState.hotkeys ~= nil then
    for _, hotkey in ipairs(runtimeState.hotkeys) do
      hotkey:delete()
    end
  end
end
```

to:

```lua
  if runtimeState.hotkeys ~= nil then
    for _, hotkey in ipairs(runtimeState.hotkeys) do
      hotkey:delete()
    end
  end
  if runtimeState.windowFilter ~= nil then
    runtimeState.windowFilter:unsubscribeAll()
    runtimeState.windowFilter = nil
  end
end
```

- [ ] **Step 2: Add windowFilter subscription in `module.start()`**

In `hammerspoon/windy.lua`, add the windowFilter after the event taps are started and before `end` of `module.start()`. Insert between `state.flagsTap:start()` and the final `end`:

```lua
  state.flagsTap:start()

  state.windowFilter = hs.window.filter.new()
  state.windowFilter:subscribe(hs.window.filter.windowCreated, function(win)
    if win == nil then
      return
    end
    local windowId = win:id()
    if windowId == nil then
      return
    end
    hs.timer.doAfter(0.5, function()
      runWindy(state, {"on-window-created", "--window-id", tostring(windowId)})
    end)
  end)
end
```

- [ ] **Step 3: Commit**

```bash
git add hammerspoon/windy.lua
git commit -m "Add Hammerspoon windowFilter for window creation detection

Global hs.window.filter watches all window creation events, waits
500ms for AX initialization, then calls windy on-window-created.
Single detection path replacing unreliable yabai signals."
```

---

### Task 6: Deploy and live verification

**Files:** None (runtime verification only)

- [ ] **Step 1: Run unit tests one final time**

Run: `cd /Users/aldo/cwd_v2/windy && python -m pytest tests/test_windy.py -v`

Expected: All 26 tests pass.

- [ ] **Step 2: Deploy Hammerspoon changes**

Run: `cd /Users/aldo/cwd_v2/windy && python -m windy install hammerspoon`

Expected: Hammerspoon reloads with the updated `windy.lua` containing the windowFilter.

- [ ] **Step 3: Reseed a space**

Run: Press ctrl+alt+space on a space with multiple windows (including Zen if available). Wait 10 seconds for reseed to complete.

Verify: `yabai -m query --windows --space | python3 -c "import json,sys; ws=json.load(sys.stdin); tiles=set(); [tiles.add(f'{w[\"frame\"][\"x\"]},{w[\"frame\"][\"y\"]},{w[\"frame\"][\"w\"]},{w[\"frame\"][\"h\"]}') for w in ws if w.get('role')=='AXWindow']; print(f'{len(tiles)} tile(s)')"`

Expected: 1 tile (all windows stacked).

- [ ] **Step 4: Test cmd+N absorption on terminal**

With the reseeded space active, focus a terminal window and press cmd+N.

Wait 2 seconds, then run the same tile count query.

Expected: Still 1 tile. The new window was absorbed into the anchor tile.

- [ ] **Step 5: Test cmd+N absorption on Zen Browser**

Focus a Zen Browser window and press cmd+N.

Wait 2 seconds, then run the tile count query.

Expected: Still 1 tile. The ghost window was rediscovered and absorbed.

- [ ] **Step 6: Test intentional split preservation**

Press ctrl+alt+v (split east). If a background window exists, it promotes into a new tile immediately. If not, press cmd+N to trigger the armed split.

Run the tile count query.

Expected: 2 tiles. The intentional split was preserved (not absorbed).

- [ ] **Step 7: Test untracked space**

Switch to a space that has NOT been reseeded. Create a new window (cmd+N).

Expected: Normal yabai BSP behavior — new window splits as usual. No absorption (space is untracked). No errors.
