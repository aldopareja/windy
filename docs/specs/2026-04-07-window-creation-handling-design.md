# Window Creation Handling (v2)

## Problem

When a new window is created (cmd+N in any app) in a tracked (reseeded) space, yabai's default BSP behavior splits the focused tile to accommodate it. The user expects new windows to stack into the focused tile, with splits triggered only by explicit user action.

A secondary issue affects apps with delayed accessibility initialization (Zen Browser). Yabai captures windows before their AX properties are ready, creating "ghost" windows that are never added to yabai's internal table and cannot be managed by any yabai command.

## Design Principles

- Single code path per event. No fallbacks. No visual artifacts.
- All window geometry and management stays in yabai. Windy remains a thin workflow wrapper.
- Fresh observation at action time. No long-running shadow state.
- Space-level absorption. Operate on the whole space, not individual windows.
- Use yabai's native mechanisms (signals, stacking) instead of fighting its visual feedback system.

## Architecture

Two layers, each building on the previous:

### Layer 1: yabai Patch (`--rediscover`)

A patch to `message.c` adds `yabai -m window --rediscover <wid>`. Given a macOS window server ID, it finds the owning application via SLS, then calls the existing `window_manager_add_existing_application_windows()` to re-scan the app's AX window list. If the window's AX properties are now available, it is added to yabai's internal table and becomes manageable.

The rescan function skips windows already in the table, making retries safe.

This eliminates ghost windows as a category. Every window can be made known to yabai.

**Invocation:** `yabai -m window --rediscover <window_server_id>`
**Success:** exit 0, no output. Window is immediately available for `--stack`, `--focus`, etc.
**Failure:** exit 1, error on stderr.
**Tiling:** The window is added to yabai's table and tiled into the current space's BSP tree, creating a new split. The caller must absorb this split if stacking is desired.

**Note:** `--rediscover` calls `window_manager_add_existing_application_windows()` which re-scans ALL windows for the owning application, not just the target. Other untracked windows from the same app are also added and tiled. The space-level absorption in `on_window_created` handles this — any resulting solo tiles are absorbed into the anchor.

### Layer 2: yabai Signal and Space-Level Absorption

A yabai signal registered at reseed time fires for every new window. The signal action calls `windy on-window-created --window-id $YABAI_WINDOW_ID`, which absorbs all stray windows in the tracked space.

**Why signals instead of `--insert stack`:** The v1 design used `yabai -m window <id> --insert stack` to proactively prevent BSP splits. This worked but `--insert` is designed for interactive one-shot use — it creates a persistent colored overlay window via `insert_feedback_show()` (view.c:8-102) to show the user where the next split will go. Using it as persistent background state created an unwanted red visual artifact. Removing `--insert stack` entirely and using reactive absorption via signals eliminates this problem.

**Why yabai signals instead of Hammerspoon `hs.window.filter`:** Yabai signals fire on the same event loop tick as BSP insertion (event_loop.c: insertion at line 566, signal push at line 571, flush at line 1629). The signal action runs in a forked subprocess, reaching the handler within ~50-100ms. The Hammerspoon watcher had a 300ms delay. Yabai signals are also native to the yabai ecosystem — no cross-tool coordination needed.

**Signal timing in yabai source (event_loop.c WINDOW_CREATED handler):**
1. Line 541: `window_manager_create_and_add_window()` — window struct created
2. Line 566: `space_manager_tile_window_on_space()` — BSP insertion (split happens here)
3. Line 571: `event_signal_push(SIGNAL_WINDOW_CREATED)` — signal queued
4. Line 1629: `event_signal_flush()` — signal actions dispatched via `fork`+`execvp`

The signal fires AFTER BSP insertion. The new window already has a tile when the handler runs.

## Signal Lifecycle

| Event | Signal Action |
|-------|--------------|
| `reseed()` | Remove existing `windy_absorb` signal (idempotent), then register new one |
| `float_space()` | Remove `windy_absorb` signal if no other tracked spaces remain |
| yabai restart | Signal is lost. User must reseed to re-register. |

The signal is global — it fires for window creation on ALL spaces. The `on_window_created` handler filters by tracked space and exits immediately for untracked spaces.

One signal handles all tracked spaces. Multiple reseeds (on same or different spaces) re-register the same labeled signal idempotently.

**Signal registration:**
```
yabai -m signal --remove windy_absorb  (ignore errors if absent)
yabai -m signal --add label=windy_absorb event=window_created \
  action='<windy_bin> on-window-created --window-id $YABAI_WINDOW_ID'
```

The `windy_bin` path is derived from the module location: `Path(__file__).resolve().parents[1] / "bin" / "windy"`. This is passed to `WorkflowRuntime` as a constructor parameter.

## The `on_window_created` Handler

The handler receives a specific `window_id` from the signal but operates on the entire space. This single code path handles both cmd+N (one window) and bulk app launches (many windows).

```
1. Rediscover window_id.
   Call yabai -m window --rediscover <wid>.
   For normal windows: no-op (already in table).
   For ghost windows: re-scans the app's AX window list.

2. Query the window and derive its space.
   If the window is not queryable after rediscovery: exit.
   If the window is not eligible: exit.
   If the space is not tracked: exit.
   If the space is not BSP: exit.

3. Snapshot the entire space.
   Query all eligible windows, group by normalized frame into tiles.
   Each tile has a visible window and zero or more background windows.

4. Reconcile pending_split.
   If pending_split was set and the anchor window's frame changed:
   the split was consumed (intentional). Clear pending_split.
   Track whether pending_split was consumed this pass.

5. Find the anchor tile.
   The anchor tile is the tile with the most windows. This is the
   original "stack pile" from the last reseed. Using window count
   is more robust than focused/recent window lookup.

6. Absorb solo tiles.
   A solo tile is a tile with exactly one window that is not the
   anchor. These are windows that yabai auto-tiled into new BSP
   splits. For each solo tile:
   - If pending_split was consumed AND the solo window is window_id:
     skip it (this is the intentional split target).
   - Otherwise: stack the solo window into the anchor tile.

7. Focus the new window if it was absorbed.
   If window_id was stacked into the anchor, focus it so it becomes
   the visible window in the tile.
```

**Key properties:**

- **Space-level:** Absorbs ALL stray windows in one pass, not just the triggering window_id.
- **Idempotent:** If a second concurrent signal runs, it builds a fresh snapshot, finds no solo tiles (already absorbed), and exits. Safe for concurrent execution.
- **pending_split aware:** When pending_split is consumed, the window_id's tile is preserved as an intentional split. All other solo tiles are still absorbed.
- **Anchor by window count:** `max(tiles, key=len(all_window_ids))`. No fragile focused/recent lookups for anchor discovery.

## Changes by Component

### yabai (patched)

**File:** `src/message.c`
**Change:** `--rediscover` command handler (16 insertions). Already deployed.
No additional yabai changes needed for v2.

### windy/yabai.py

**Remove** from `YabaiClient` Protocol and `SubprocessYabaiClient`:
- `arm_window_stack(window_id)` — no longer used

**Add** to `YabaiClient` Protocol and `SubprocessYabaiClient`:
- `add_signal(label, event, action)`: wraps `yabai -m signal --add label=<label> event=<event> action=<action>`. The action string may contain spaces and `$YABAI_WINDOW_ID`; the implementation must ensure correct quoting so yabai's tokenizer treats the entire action as one value.
- `remove_signal(label)`: wraps `yabai -m signal --remove <label>`. Raises `WorkflowError` on failure (caller catches if signal doesn't exist).

### windy/workflow.py

**Modify constructor** to accept `windy_bin: str` parameter. Store as `self._windy_bin`.

**Modify `reseed()`:**
- Remove `arm_window_stack` call.
- After writing state, register the yabai signal:
  ```python
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

**Modify `split()`:**
- Remove `arm_window_stack` call from the promote path.

**Modify `delete_tile()`:**
- Remove `arm_window_stack` call.

**Modify `float_space()`:**
- After deleting the space from state, if no tracked spaces remain, remove the signal:
  ```python
  updated_state = _delete_space_state(context.state, context.workflow_space)
  self._state_store.write(updated_state)
  if not updated_state.spaces:
      try:
          self._yabai.remove_signal("windy_absorb")
      except WorkflowError:
          pass
  ```

**Replace `on_window_created()`:** Complete rewrite with space-level absorption (as described above).

**Add helper:**
```python
def _find_anchor_tile(snapshot: _LiveSpaceSnapshot) -> Optional[LiveTile]:
    if not snapshot.tiles:
        return None
    return max(snapshot.tiles, key=lambda t: len(t.all_window_ids))
```

### windy/cli.py

**Modify constructor call** to pass `windy_bin`:
```python
windy_bin = str(Path(__file__).resolve().parents[1] / "bin" / "windy")
runtime = WorkflowRuntime(
    yabai=yabai,
    hammerspoon=hammerspoon,
    state_store=state_store,
    windy_bin=windy_bin,
)
```

The `on-window-created` subcommand and its dispatch are unchanged.

### windy/hammerspoon/windy.lua

**Remove** the `hs.window.filter` subscription (the block that creates `wf`, subscribes to `windowCreated`, and stores `state.windowFilter`).

**Remove** the `windowFilter` cleanup from `stopExisting()`.

### tests/test_windy.py

**FakeYabaiClient:**
- Remove `arm_window_stack` method.
- Add `add_signal(label, event, action)` and `remove_signal(label)` methods.

**WorkflowRuntime construction in tests:** Pass `windy_bin="/test/bin/windy"`.

**Remove tests:**
- `test_reseed_arms_stack_insertion_on_focused_window`
- `test_split_promote_arms_stack_insertion`
- `test_delete_tile_arms_stack_insertion`

**Update tests:**
- `test_reseed_tracks_space_and_stacks_other_windows`: expect signal registration instead of arm_stack.
- `test_split_with_background_in_focused_tile_promotes_candidate`: remove arm_stack from expected actions.
- `test_delete_tile_merges_focused_tile_into_recent_sibling`: remove arm_stack from expected actions.
- All `on_window_created` tests: update for space-level absorption, anchor-by-count, no arm_stack at end.

**Add tests:**
- `test_on_window_created_absorbs_multiple_solo_tiles`: 3 solo tiles absorbed in one pass.
- `test_on_window_created_idempotent_when_no_solo_tiles`: second signal finds nothing to do.
- `test_reseed_registers_signal`: verify signal add after reseed.
- `test_float_removes_signal_when_last_tracked_space`: verify signal removal.

## State Model

No new persistent state. The tracked state per space remains:

```
TrackedSpaceState:
  workflow_space: EligibleWorkflowSpace  (display + space)
  pending_split: Optional[PendingSplit]  (direction + anchor_window_id + anchor_frame)
```

The yabai signal is yabai-managed state (stored in yabai's signal list, not in windy's state file). It is registered explicitly at reseed time and removed when no tracked spaces remain.

## Preserved Behavior

All existing workflows from priorities.md are preserved:

1. **Reseed** (ctrl+alt+space): Unchanged behavior. Now also registers the yabai signal.
2. **Split** (ctrl+alt+h/v): Unchanged. Promote path no longer arms stack. Arm-for-next-window path unchanged.
3. **Re-reseed**: Unchanged. Signal re-registered idempotently.
4. **AltTab swapping**: Unchanged.
5. **Float space** (ctrl+alt+f): Unchanged behavior. Signal removed if last tracked space.
6. **Delete tile** (ctrl+alt+d): Unchanged. No longer arms stack.
7. **Navigation** (ctrl+alt+arrows): Unchanged.

## Edge Cases

**Single cmd+N:** One solo tile found. Absorbed into anchor. Window focused.

**Rapid successive cmd+N:** Each creates a solo tile. Each signal fires. First signal absorbs ALL solo tiles (space-level). Subsequent signals find no solo tiles and exit (idempotent).

**Bulk app launch (Zen restoring 5 windows):** 5 solo tiles created. 5 signals fire concurrently. First to complete absorbs all 5. Others find nothing to do.

**cmd+N in untracked space:** Signal fires, `on_window_created` reads state, space not tracked, exits. Normal yabai BSP behavior preserved.

**Intentional split + new window:** pending_split consumed. The window_id's solo tile is preserved (intentional split). Other solo tiles absorbed.

**Window destroyed before signal fires:** `on_window_created` queries the window, gets None, exits.

**yabai restart:** Signal is lost. Space is still tracked in windy state. New windows will split until user reseeds (which re-registers the signal).

**Ghost window (Zen Browser):** `--rediscover` adds it to yabai's table. Then space-level absorption stacks it into the anchor like any other window.

**--rediscover side effect (adds all app windows):** If the ghost app has other untracked windows, they all get added to the BSP tree. The space-level absorption handles them — any that created solo tiles are absorbed into the anchor.

## Trade-off

Every new window briefly appears as a BSP split for ~50-100ms before the signal handler absorbs it. This is:
- Much faster than the v1 Hammerspoon delay (300ms)
- Far less objectionable than the v1 persistent red overlay (indefinite)
- Consistent — same behavior for every window, no first-vs-subsequent distinction
- Zero visual artifacts when no new windows are being created

## Build and Deploy

The yabai `--rediscover` patch is already built and deployed. No additional yabai changes are needed for v2.

The windy changes (Python + Lua) do not require compilation.

After modifying windy, reload Hammerspoon to pick up the Lua changes:
```bash
cd windy && python3 -c "from windy.cli import main; main(['install', 'hammerspoon'])"
```
